import base64
import json
import os
from pathlib import Path
import time
from typing import Any, Collection, Dict, List, Optional, Set, Tuple, Union
from uuid import uuid4

import requests


class Client:
    def __init__(self, **kwargs):
        """
        create a client instance.
        :param kwargs:
            - kafka_rest_api_url: API Endpoint URL. Required if "KAFKA_REST_API_URL" is not an environment variable.
            - topic_id: target topic_id. required if "TOPIC_ID" is not an environment variable.
            - auth_headers: Authentication headers as a Dict[str, str].
        """

        self.kafka_rest_api_url = os.environ.get('KAFKA_REST_API_URL', kwargs.get("kafka_rest_api_url"))
        if not self.kafka_rest_api_url:
            raise Exception("Invalid value assigned to 'kafka_rest_api_url' or 'KAFKA_REST_API_URL' env var.")

        self.topic_id = os.environ.get('TOPIC_ID', kwargs.get("topic_id"))
        if not self.topic_id:
            raise Exception("Invalid value assigned to 'topic_id' or 'TOPIC_ID' env var.")

        self.auth_headers = kwargs.get("auth_headers", dict())

        self.username_password = kwargs.get("username_password", None)

        if self.username_password:
            self.auth_headers['Authorization'] = self.generate_basic_auth(self.username_password[0],self.username_password[1])

        x_api_key = os.environ.get("X_API_KEY")

        if x_api_key and "x-api-key" not in self.auth_headers:
            self.auth_headers.update({"x-api-key": x_api_key})


    def generate_basic_auth(self, username, password):
        token = base64.b64encode(f"{username}:{password}".encode('utf-8')).decode("ascii")
        return f'Basic {token}'


    def request(self, **kwargs):
        kwargs["url"] = f"{self.kafka_rest_api_url}{kwargs['url']}"
        if self.auth_headers:
            kwargs.get("headers", {}).update(self.auth_headers)

        response = requests.request(**kwargs)

        if not response.ok:
            response.raise_for_status()

        return response


class Producer(Client):
    def __init__(self, producer_data_max_size: int = 67_108_864, **kwargs):
        """
        Create a producer instance.
        :param producer_data_max_size: Maximum size of each request payload in bytes.
        :param kwargs:
            - kafka_rest_api_url: API Endpoint URL. Required if "KAFKA_REST_API_URL" is not an environment variable.
            - topic_id: target topic_id. required if "TOPIC_ID" is not an environment variable.
            - auth_headers: Authentication headers as a Dict[str, str].
        """
        super().__init__(**kwargs)

        self.contect_type = kwargs.get("contect_type", "application/vnd.kafka.jsonschema.v2+json")

        self.max_data_bytes = os.environ.get('PRODUCER_DATA_MAX_SIZE', producer_data_max_size)
        self.key_history, self.key_last_request = [], []

    @staticmethod
    def __manage_keys(len_messages: int, keys: Optional[List[str]] = None):
        if not keys:
            return [str(uuid4()) for _ in range(len_messages)]

        elif len(keys) != len_messages:
            raise ValueError("List of keys must have the same size as list of messages.")

        return keys

    def produce(self, messages: Collection, endpoint: str, keys: Optional[List[str]] = None, key_schema: Optional[str] = None, value_schema: Optional[str] = None) -> List[str]:
        """
        Produce messages to a given topic.
        :param messages: JSON serializable Collection of messages.
        :param endpoint: Target endpoint.
        :param keys: Optional list of customized keys. Number of keys must match the number of messages.
        :return: List of generated UUID keys.
        """
        headers = {
                    "Content-Type": self.contect_type
                }
        keys = self.__manage_keys(len(messages), keys)
        records = {
            "records": [
            {"key": k, "value": v} for k, v in zip(keys, messages)]}

        if key_schema!=None:
            records["key_schema"]=key_schema
        if value_schema!=None:
            records["value_schema"]=value_schema

        record_data = json.dumps(records)

        self._check_data_size(record_data.encode("utf-8"))

        self.request(method="POST", url=f"/topics/{self.topic_id}", headers=headers, data=record_data)

        self.key_history.extend(keys)
        self.key_last_request = keys
        return self.key_last_request

    def _check_data_size(self, data: bytes):
        if self.max_data_bytes < len(data):
            raise RuntimeError(f"Producer request data exceeded allowed number bytes: {self.max_data_bytes} bytes")

    def produce_files(self, files: List[str],
                      endpoint: str,
                      keys: Optional[List[str]] = None) -> List[str]:
        """
        Produce files to a given topic.
        :param files: List of paths to input files.
        :param endpoint: Target API endpoint.
        :param keys: Optional list of customized keys. Number of keys must match the number of messages.
        :return: List of generated UUID keys.
        """

        messages = []
        for path in files:
            with open(f"{path}", "rb") as f:
                messages.append(
                    {
                        "name": Path(path).name,
                        "bytes":  base64.b64encode(f.read()).decode(),
                        "type": f"application/{'pdf' if path.endswith('.pdf') else 'octet-stream'}"
                    }
                )

        return self.produce(messages, endpoint, keys=keys)


class Consumer(Client):
    def __init__(self, **kwargs):
        """
        Create a consumer instance.
        :param kwargs:
            - kafka_rest_api_url: API Endpoint URL. Required if "KAFKA_REST_API_URL" is not an environment variable.
            - topic_id: target topic_id. required if "TOPIC_ID" is not an environment variable.
            - auth_headers: Authentication headers as a Dict[str, str].
            - consumer_group: Assign a given consumer group name. Defaults to a randomly generated UUID is assigned.
            - instance: Assign a given instance name. Defaults to a randomly generated UUID.
        """
        super().__init__(**kwargs)

        self.created = False
        self.consumer_group = kwargs.get("consumer_group", str(uuid4()).replace("-", ""))
        self.instance = kwargs.get("instance", str(uuid4()).replace("-", ""))
        self.format = kwargs.get("format", "binary")
        self.accept = kwargs.get("accept", 'application/vnd.kafka.binary.v2+json')
        self.request_timeout = kwargs.get("request_timeout", 11000)
        self.fetch_min_bytes = kwargs.get("fetch_min_bytes", 100000)
        self.auto_offset_reset = kwargs.get("auto_offset_reset", "earliest") 

        self.remaining_keys = set()

    def __enter__(self):
        return self.create().subscribe()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.delete()

    def create(self):
        """
        Create a Consumer instance in binary format.
        :return: self.
        """

        if not self.created:
            url = f"/consumers/{self.consumer_group}"
            headers = {"Content-Type": "application/vnd.kafka.json.v2+json"}
            
            config = json.dumps(
                {   
                    "name": self.instance, 
                    "format": self.format, 
                    "auto.offset.reset": self.auto_offset_reset, 
                    "consumer.request.timeout.ms": self.request_timeout,
                    "fetch.min.bytes": self.fetch_min_bytes,
                })

            self.request(method="POST", url=url, headers=headers, data=config)
            self.created = True

        return self

    def subscribe(self):
        """
        Subscribe to the given topics.
        :return: self.
        """

        url = f"/consumers/{self.consumer_group}/instances/{self.instance}/subscription"
        headers = {"Content-Type": "application/vnd.kafka.json.v2+json"}
        topics_data = json.dumps({"topics": [f"{self.topic_id}"]})

        self.request(method="POST", url=url, headers=headers, data=topics_data)
        return self


    def assign(self, partitions):
        """
        Assign a given partitions.
        :return: self.
        """

        url = f"/consumers/{self.consumer_group}/instances/{self.instance}/assignments"
        headers = {"Content-Type": "application/vnd.kafka.json.v2+json"}
        partitions_data = json.dumps(partitions)

        self.request(method="POST", url=url, headers=headers, data=partitions_data)
        return self


    def consume_earliest(self, max_bytes: int = None, timeout: int = None) -> List[Dict[str, Any]]:
        """
        Consume the earliest messages in the assigned topics.
        :return: List of dictionaries where the "value" key contains the message and the "key" key contains its key.
        """
        url = f'/consumers/{self.consumer_group}/instances/{self.instance}/records'
        headers = {'Accept': self.accept}

        params={}

        if max_bytes is not None:
            params["max_bytes"]=max_bytes

        if timeout is not None:
            params["timeout"]=timeout

        response = self.request(method="GET", url=url, headers=headers, data="", params=params)
        response_decoded = [self.parse_record(r) for r in response.json()]

        return response_decoded

    def consume_latest(self) -> List[Dict[str, Any]]:
        """
        Consume the latest messages in the assigned topics.
        :return: List of dictionaries where the "value" key contains the message and the "key" key contains its key.
        """


        partitions = self.partitions(self.topic_id)
        # get the first partition id? (not sure this is needed as we have manually assigned it already)
        partid=partitions[0]["partition"]        

        # we need to use assign instead of subscribe
        # so we can get the latest event from the topic
        self.assign({
                "partitions": [
                    {
                    "topic": self.topic_id,
                    "partition": partid
                    }
                ]})


        response_decode=None

        retry=10

        # ok, we need to start at the latest 
        # and look back for one that can be used
        while retry>0:
            try:
                
                offsets = self.offsets(self.topic_id, partid)
                # and now we have the latest offset
                latestoffset=offsets["end_offset"]
                
                if latestoffset<=0:
                    break

                latestoffset=latestoffset-1
                self.seek(self.topic_id, partid, latestoffset)

                # consume the earliest entry until there is no more data
                # so we can get the latest event from the topic
                for message in self.consume_earliest():
                    response_decode=message

                if response_decode is not None: 
                    break

            except requests.exceptions.HTTPError as e:
                # and retry...
                pass

            retry=retry-1


            
        return response_decode

    def consume_all_raw(self) -> List[Dict[str, Any]]:
        """
        Consume the earliest messages in the assigned topics.
        :return: List of dictionaries where the "value" key contains the message and the "key" key contains its key.
        """
        url = f'/consumers/{self.consumer_group}/instances/{self.instance}/records'
        headers = {'Accept': self.accept}

        response = self.request(method="GET", url=url, headers=headers, data="")
        response_decoded = response.json()

        return response_decoded        


    def offsets(self, topic, partition):
        """
        Get the offsets for a topic
        :return: self.
        """

        url = f"/topics/{topic}/partitions/{partition}/offsets"
        headers = {"Content-Type": "application/vnd.kafka.json.v2+json"}

        response=self.request(method="GET", url=url, headers=headers)

        return response.json()

        
    def partitions(self, topic):
        """
        Get the partitions for a topic
        :return: partitions.
        """

        url = f"/topics/{topic}/partitions"
        headers = {"Content-Type": "application/vnd.kafka.json.v2+json"}

        response=self.request(method="GET", url=url, headers=headers)

        return response.json()


    def seek(self, topic, position, offset):
        """
        Position to move to
        :return: self.
        """

        url = f"/consumers/{self.consumer_group}/instances/{self.instance}/positions"
        headers = {"Content-Type": "application/vnd.kafka.json.v2+json"}
        payload_data = json.dumps(
                        {
                            "offsets": [
                                {
                                    "topic": topic,
                                    "partition": position,
                                    "offset": offset
                                }
                            ]
                        })

        res=self.request(method="POST", url=url, headers=headers, data=payload_data)

        return self


    def delete(self):
        """
        Delete the current client instance from the kafka cluster.
        """
        url = f'/consumers/{self.consumer_group}/instances/{self.instance}'
        headers = {'Content-Type': 'application/vnd.kafka.v2+json'}

        self.request(method="DELETE", url=url, headers=headers, data="")
        self.created = False

    def consume(self, keys: List[str], interval_sec: Union[int, float] = 5) -> Tuple[List[dict], Set[str]]:
        """
        Consume messages from the assigned topics as iterator.
        :param keys: List of keys to choose from the topics.
        :param interval_sec: Minimum interval in seconds between polling requests.
        :return: List of dictionaries where the "value" key contains the message and the "key" key contains its key.
        """

        if interval_sec < 0:
            raise ValueError("'interval_sec' should be an 'int' or 'float' greater or equal to 0.")

        self.remaining_keys = set(keys)

        while self.remaining_keys:
            time.sleep(interval_sec)
            data = {d['key']: d for d in self.consume_earliest() if d['key'] in self.remaining_keys}
            self.remaining_keys = self.remaining_keys - set(data)
            yield [d for _, d in data.items()], self.remaining_keys

    def consume_all(self, keys: List[str], interval_sec: Union[int, float] = 5) -> List[Dict[str, Any]]:
        """
        Consume all messages from all keys.
        :param keys: List of keys to choose from the topics.
        :param interval_sec: Minimum interval in seconds between polling requests.
        :return: List of dictionaries where the "value" key contains the message and the "key" key contains its key.
        """

        return [data for data, _ in self.consume(keys, interval_sec)]

    @staticmethod
    def decode_base64(string: str):
        if isinstance(string, dict):
            # already parsed
            return string
        elif isinstance(string, str):
            try:
                # try decoding as pure json first
                return json.loads(string)
            except json.decoder.JSONDecodeError:
                try:
                    # try decoding as base64 encoded json
                    decode=base64.b64decode(string)
                    return json.loads(decode)
                except UnicodeDecodeError:
                    # decode as string
                    return(string)
                except json.decoder.JSONDecodeError:
                    # try decoding as base64 data
                    return base64.b64decode(string)
        return string

    @staticmethod
    def parse_record(record: dict):
        return {
            "key": Consumer.decode_base64(record["key"]),
            "value": Consumer.decode_base64(record["value"])
        }
