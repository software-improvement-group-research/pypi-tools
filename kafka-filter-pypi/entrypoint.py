import time
import json
import argparse
import datetime
import dateutil.parser

from pkg_resources import Requirement

from kafka import KafkaConsumer, KafkaProducer

class PyPIFilter:
    def __init__(self, in_topic, out_topic, bootstrap_servers, group, check_old):
        self.in_topic = in_topic
        self.out_topic = out_topic
        self.bootstrap_servers = bootstrap_servers.split(",")
        self.group = group
        self.check_old = check_old

        self.packages = {}
        if self.check_old:
            self._fill_old()

    def consume(self):
        self._init_kafka()

        for message in self.consumer:
            package = message.value
            print ("{}: Consuming {}".format(
                datetime.datetime.now(),
                package["title"]
            ))

            for entry in self._extract(package):
                if not self._exists(entry):
                    self._store(entry)
                    self.produce(entry)
                else:
                    print ("{} already exists".format(entry))

    def produce(self, entry):
        self.producer.send(self.out_topic, json.dumps(entry))

    def _exists(self, entry):
        if not entry["product"] in self.packages:
            return False
        if not entry["version"] in self.packages[entry["product"]]:
            return False
        return True

    def _store(self, entry):
        if not entry["product"] in self.packages:
            self.packages[entry["product"]] = set()
        self.packages[entry["product"]].add(entry["version"])

    def _init_kafka(self):
        self.consumer = KafkaConsumer(
            self.in_topic,
            bootstrap_servers=self.bootstrap_servers,
            # consume earliest available messages
            auto_offset_reset='earliest',
            enable_auto_commit=True,
            group_id=self.group,
            # messages are raw bytes, decode
            value_deserializer=lambda x: json.loads(x.decode('utf-8'))
        )

        self.producer = KafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda x: x.encode('utf-8')
        )

    def _fill_old(self):
        consumer = KafkaConsumer(
            self.out_topic,
            bootstrap_servers=self.bootstrap_servers,
            auto_offset_reset='earliest',
            # set timeout so we don't get stuck on old messages
            consumer_timeout_ms=1000,
            value_deserializer=lambda x: json.loads(x.decode('utf-8'))
        )
        for message in consumer:
            pkg = message.value
            if not self._exists(pkg):
                self._store(pkg)

    def _extract(self, package):
        try:
            pkg_name = package["project"]["info"]["name"]
            requires_dist = package["project"]["info"]["requires_dist"]
            releases = package["project"]["releases"]
        except KeyError:
            print ("Could not retrieve packaging info from {}".format(json.dumps(package)))
            return

        requires_dist = self._parse_requires(requires_dist)
        for release in releases:
            if not release.get("version", None) or\
                    not release.get("releases", None):
                continue

            version = release["version"]
            ts = float("inf")

            # get smallest value for timestamp
            for r in release["releases"]:
                if not r.get("upload_time", None):
                    continue

                candts = int(dateutil.parser.isoparse(r["upload_time"]).timestamp())
                if candts < ts:
                    ts = candts

            if ts == float("inf"):
                print ("Did not find a valid timestamp on {}".format(releases))
                continue

            entry = {
                "product": pkg_name,
                "version": version,
                "version_timestamp": ts,
                "requires_dist": requires_dist
            }
            yield entry

    def _parse_requires(self, requires):
        parsed = []
        for r in requires:
            req = Requirement.parse(r)

            product = req.name
            specs = req.specs

            constraints = []
            def add_range(begin, end):
                if begin and end:
                    if begin[1] and end[1]:
                        constraints.append(f"[{begin[0]}..{end[0]}]")
                    elif begin[1]:
                        constraints.append(f"[{begin[0]}..{end[0]})")
                    elif end[1]:
                        constraints.append(f"({begin[0]}..{end[0]}]")
                    else:
                        constraints.append(f"({begin[0]}..{end[0]})")
                elif begin:
                    if begin[1]:
                        constraints.append(f"[{begin[0]}..]")
                    else:
                        constraints.append(f"({begin[0]}..]")
                elif end:
                    if end[1]:
                        constraints.append(f"[..{end[0]}]")
                    else:
                        constraints.append(f"[..{end[0]})")

            begin = None
            end = None
            for key, val in sorted(specs, key=lambda x: x[1]):
                # if begin, then it is already in a range
                if key == "==":
                    if begin and end:
                        add_range(begin, end)
                        begin = None
                        end = None
                    if not begin:
                        constraints.append(f"[{val}]")

                if key == ">":
                    if end:
                        add_range(begin, end)
                        end = None
                        begin = None
                    if not begin:
                        begin = (val, False)
                if key == ">=":
                    if end:
                        add_range(begin, end)
                        begin = None
                        end = None
                    if not begin:
                        begin = (val, True)

                if key == "<":
                    end = (val, False)
                if key == "<=":
                    end = (val, True)
            add_range(begin, end)

            parsed.append({"forge": "PyPI", "product": product, "constraints": constraints})

        return parsed


def get_parser():
    parser = argparse.ArgumentParser(
        """
        Consume PyPI packaging information from a Kafka topic and
        filter it to remove duplicate versioning information.
        Produce unique package-version tuples to the output topic.
        """
    )
    parser.add_argument(
        'in_topic',
        type=str,
        help="Kafka topic to read from."
    )
    parser.add_argument(
        'out_topic',
        type=str,
        help="Kafka topic to write unique package-version tuples."
    )
    parser.add_argument(
        'bootstrap_servers',
        type=str,
        help="Kafka servers, comma separated."
    )
    parser.add_argument(
        'group',
        type=str,
        help="Kafka consumer group to which the consumer belongs."
    )
    parser.add_argument(
        'sleep_time',
        type=int,
        help="Time to sleep inbetween each scrape (in sec)."
    )
    parser.add_argument(
        '--check-old',
        dest="check_old",
        action='store_true',
        help="Read messages from the output topic before consuming."
    )
    return parser

def main():
    parser = get_parser()
    args = parser.parse_args()

    in_topic = args.in_topic
    out_topic = args.out_topic
    bootstrap_servers = args.bootstrap_servers
    group = args.group
    sleep_time = args.sleep_time
    check_old = args.check_old

    pypi_filter = PyPIFilter(
        in_topic, out_topic, bootstrap_servers,
        group, check_old)

    while True:
        pypi_filter.consume()

        time.sleep(sleep_time)
        check_old = False

if __name__ == "__main__":
    main()
