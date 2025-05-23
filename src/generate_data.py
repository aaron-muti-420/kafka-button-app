#!/usr/bin/env python3

"""Generate fake data."""

import argparse
import asyncio
import datetime
import logging
import os
import pathlib
import random
import sys

from typing import Iterator, Optional, Any

import dotenv

from aiokafka import AIOKafkaProducer
from aiokafka.helpers import create_ssl_context
import avro.schema
from geoip2fast import GeoIP2Fast

from message_support import DEFAULT_TOPIC_NAME as TOPIC_NAME
from message_support import Action
from message_support import Event
from message_support import new_cookie
from message_support import load_geoip_data
from message_support import create_avro_schema
from message_support import get_parsed_avro_schema
from message_support import register_avro_schema
from message_support import make_avro_payload


# Since geoip2fast has IP address generation methods, we don't need Faker
# The DOWNSIDE is that the geoip2fast methods only generate IP addresses that
# exist in the GeoIP2Fast data files, so we won't get any unrecognised addresses.
#
# This is probably OK for at least intial testing, but does mean we won't
# know what might happen in all Real Life cases.
#
#from faker import Faker
#from faker.providers import internet

#fake = Faker()
#fake.add_provider(internet)

logging.basicConfig(level=logging.INFO)

# However, httpx will log all GET and POST requests at level INFO,
# includingthe full URI, with any embedded passwords :(
# We definitely want to disable that - for instance it would
# show our Karapace password when registering a schema
logging.getLogger('httpx').setLevel(logging.ERROR)


# Command line default values
DEFAULT_CERTS_FOLDER = "certs"
# Allow setting these defaults via a `.env` file as well
dotenv.load_dotenv()
KAFKA_SERVICE_URI = os.getenv("KAFKA_SERVICE_URI", "localhost:9093")
SCHEMA_REGISTRY_URI = os.getenv("SCHEMA_REGISTRY_URI", None)

# Default max presses per session
DEFAULT_MAX_PRESSES = 10

# A `cohort` value of None means that the data comes from this data generator
FAKE_DATA_COHORT = None


class FakeEventCreator:
    """A way of creating a sequence of linked events, with shared data.
    """

    def __init__(self, geoip: GeoIP2Fast, cohort: Optional[int]=None):
        """Perform the basic setup of a sequence of session events.

        * `geoip` is our GeoIP2Fast instance, which we use to look up IP addresses
          and get back location data
        * `cohort` is a way of identifying a group in which that person is placed
          (one assumes a cohort of experimental subjects). The default it None.
          A value of None means that this data is produced by the fake data
          generator script.
        """
        self.geoip = geoip
        self.cookie = new_cookie(geoip, self.get_fake_ip_address, cohort=cohort)
        logging.info(f'New "session" {self.cookie}')

        # We work out the *real* now, but then fuzz it a bit so that
        # successive fake sessions don't follow each other in a linear
        # sequence
        self.now = datetime.datetime.now(datetime.timezone.utc)
        fuzz = datetime.timedelta(seconds=random.randint(-2*60, 0))
        self.now += fuzz

    def get_fake_ip_address(self, request: Any) -> str:
        """This fake IP address creator doesn't need a "Request" parameter.
        """
        if random.randint(1,3) == 3:    # or some other distribution
            ip_address = self.geoip.generate_random_ipv6_address()
        else:
            ip_address = self.geoip.generate_random_ipv4_address()
        logging.info(f'Using fake IP address {ip_address}')
        return ip_address

    def new_event(self, action: Action, elapsed_ms: int) -> Event:
        """Move time fowards and return an Event
        """
        self.now = self.now + datetime.timedelta(milliseconds=elapsed_ms)
        microseconds_since_epoch = int(self.now.timestamp() * 1_000_000)

        return Event(
            **dict(self.cookie),
            timestamp=microseconds_since_epoch,
            action = str(action),
        )

    def enter_page(self, elapsed_ms: int) -> Event:
        """Return our page entry event"""
        return self.new_event(Action.ENTER_PAGE, elapsed_ms)

    def press_button(self, elapsed_ms: int) -> Event:
        """Return a button press event"""
        return self.new_event(Action.PRESS_BUTTON, elapsed_ms)


def generate_session(geoip: GeoIP2Fast, max_presses: int) -> Iterator[Event]:
    """Yield button press message tuples from a single web app "session"

    Note we do *not* expose the IP address, as that counts as personal information.
    If we don't yield it in our datastructure, then there's no way we can leak it.
    """
    # I can't see a way of getting the lat, long for a city without using an internet
    # connection, so let's not do that, at least for the moment. The consumer end can
    # worry about that.

    event_creator = FakeEventCreator(geoip, cohort=FAKE_DATA_COHORT)

    # We start with an EnterPage event
    yield event_creator.enter_page(0)

    # Luckily we're not trying to be especially random, so this is good enough
    number_presses = random.randint(1, max_presses)
    for press in range(number_presses):
        elapsed_ms = random.randint(500, 5000)
        yield event_creator.press_button(elapsed_ms)

    logging.info('Left page')


async def send_messages_to_kafka(
        kafka_uri: str,
        certs_dir: pathlib.Path,
        topic_name: str,
        schema_id: int,
        parsed_schema: avro.schema.RecordSchema,
        geoip: GeoIP2Fast,
        num_sessions: int,
        max_presses: int,
):
    ssl_context = create_ssl_context(
        cafile=certs_dir / "ca.pem",
        certfile=certs_dir / "service.cert",
        keyfile=certs_dir / "service.key",
    )

    producer = AIOKafkaProducer(
        bootstrap_servers=kafka_uri,
        security_protocol="SSL",
        ssl_context=ssl_context,
    )

    await producer.start()

    try:
        for count in range(num_sessions):
            logging.info(f'Session {count+1} of {num_sessions}')
            for event in generate_session(geoip, max_presses):
                # Don't tell the `to_str` method our session id, as we want to see it
                # printed out (and *of course* it will be "our" session id!)
                print(event.to_str(''))
                raw_bytes = make_avro_payload(event, schema_id, parsed_schema)
                # For the moment, don't let it buffer messages
                await producer.send_and_wait(topic_name, raw_bytes)
    finally:
        await producer.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-k', '--kafka-uri', default=KAFKA_SERVICE_URI,
        help='the URI for the Kafka service, defaulting to $KAFKA_SERVICE_URI'
        ' if that is set',
    )
    parser.add_argument(
        '-d', '--certs-dir', default=DEFAULT_CERTS_FOLDER, type=pathlib.Path,
        help=f'directory containing the ca.pem, service.cert and service.key'
        ' files, default "{DEFAULT_CERTS_FOLDER}"',
    )
    parser.add_argument(
        '-s', '--schema-uri', default=SCHEMA_REGISTRY_URI,
        help='the URI for the Karapace schema registry, defaulting to'
        ' $SCHEMA_REGISTRY_URI if that is set',
        )
    parser.add_argument(
        '--forever', action='store_true',
        help='generate fake "sessions" \'forever\''
        f' (actually equivalent to `--num-sessions {sys.maxsize}`)'
        )
    parser.add_argument(
        '-n', '--num-sessions', type=int, default=1,
        help='the URI for the Karapace schema registry, defaulting to'
        ' $SCHEMA_REGISTRY_URI if that is set',
        )
    parser.add_argument(
        '-p', '--max-presses', type=int, default=DEFAULT_MAX_PRESSES,
        help=f'the maximum button presses per session, default {DEFAULT_MAX_PRESSES}'
        )

    args = parser.parse_args()

    if args.kafka_uri is None:
        print('The URI for the Kafka service is required')
        print('Set KAFKA_SERVICE_URI or use the -k switch')
        logging.error('The URI for the Kafka service is required')
        logging.error('Set KAFKA_SERVICE_URI or use the -k switch')
        return -1

    if args.schema_uri is None:
        print('The URI for the Karapace schema registry is required')
        print('Set SCHEMA_REGISTRY_URI or use the -s switch')
        logging.error('The URI for the Karapace schema registry is required')
        logging.error('Set SCHEMA_REGISTRY_URI or use the -s switch')
        return -1

    if args.num_sessions <= 0:
        print(f'The `--num-sessions` argument must be 1 or more, not {args.num_sessions}')
        logging.error(f'The `--num-sessions` argument must be 1 or more, not {args.sessions}')
        return -1

    geoip = load_geoip_data()

    schema = create_avro_schema(TOPIC_NAME)

    # Parsing the schema both validates it, and also puts it into a form that
    # can be used when envoding/decoding message data
    parsed_schema = get_parsed_avro_schema(schema)

    schema_id = register_avro_schema(args.schema_uri, TOPIC_NAME, schema)

    if args.forever:
        args.num_sessions = sys.maxsize

    with asyncio.Runner() as runner:
        runner.run(
            send_messages_to_kafka(
                args.kafka_uri, args.certs_dir, TOPIC_NAME, schema_id, parsed_schema, geoip,
                args.num_sessions, args.max_presses,
            ),
        )


if __name__ == '__main__':
    main()
