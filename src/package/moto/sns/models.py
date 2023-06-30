import datetime
import json
import requests
import re

from collections import OrderedDict
from typing import Any, Dict, List, Iterable, Optional, Tuple

from moto.core import BaseBackend, BackendDict, BaseModel, CloudFormationModel
from moto.core.utils import (
    iso_8601_datetime_with_milliseconds,
    camelcase_to_underscores,
)
from moto.moto_api._internal import mock_random
from moto.sqs import sqs_backends
from moto.sqs.exceptions import MissingParameter

from .exceptions import (
    SNSNotFoundError,
    TopicNotFound,
    DuplicateSnsEndpointError,
    SnsEndpointDisabled,
    SNSInvalidParameter,
    InvalidParameterValue,
    InternalError,
    ResourceNotFoundError,
    TagLimitExceededError,
    TooManyEntriesInBatchRequest,
    BatchEntryIdsNotDistinct,
)
from .utils import make_arn_for_topic, make_arn_for_subscription, is_e164


DEFAULT_PAGE_SIZE = 100
MAXIMUM_MESSAGE_LENGTH = 262144  # 256 KiB
MAXIMUM_SMS_MESSAGE_BYTES = 1600  # Amazon limit for a single publish SMS action


class Topic(CloudFormationModel):
    def __init__(self, name: str, sns_backend: "SNSBackend"):
        self.name = name
        self.sns_backend = sns_backend
        self.account_id = sns_backend.account_id
        self.display_name = ""
        self.delivery_policy = ""
        self.kms_master_key_id = ""
        self.effective_delivery_policy = json.dumps(DEFAULT_EFFECTIVE_DELIVERY_POLICY)
        self.arn = make_arn_for_topic(self.account_id, name, sns_backend.region_name)

        self.subscriptions_pending = 0
        self.subscriptions_confimed = 0
        self.subscriptions_deleted = 0
        self.sent_notifications: List[
            Tuple[str, str, Optional[str], Optional[Dict[str, Any]], Optional[str]]
        ] = []

        self._policy_json = self._create_default_topic_policy(
            sns_backend.region_name, self.account_id, name
        )
        self._tags: Dict[str, str] = {}
        self.fifo_topic = "false"
        self.content_based_deduplication = "false"

    def publish(
        self,
        message: str,
        subject: Optional[str] = None,
        message_attributes: Optional[Dict[str, Any]] = None,
        group_id: Optional[str] = None,
        deduplication_id: Optional[str] = None,
    ) -> str:
        message_id = str(mock_random.uuid4())
        subscriptions, _ = self.sns_backend.list_subscriptions(self.arn)
        for subscription in subscriptions:
            subscription.publish(
                message,
                message_id,
                subject=subject,
                message_attributes=message_attributes,
                group_id=group_id,
                deduplication_id=deduplication_id,
            )
        self.sent_notifications.append(
            (message_id, message, subject, message_attributes, group_id)
        )
        return message_id

    @classmethod
    def has_cfn_attr(cls, attr: str) -> bool:
        return attr in ["TopicName"]

    def get_cfn_attribute(self, attribute_name: str) -> str:
        from moto.cloudformation.exceptions import UnformattedGetAttTemplateException

        if attribute_name == "TopicName":
            return self.name
        raise UnformattedGetAttTemplateException()

    @property
    def physical_resource_id(self) -> str:
        return self.arn

    @property
    def policy(self) -> str:
        return json.dumps(self._policy_json, separators=(",", ":"))

    @policy.setter
    def policy(self, policy: Any) -> None:  # type: ignore[misc]
        self._policy_json = json.loads(policy)

    @staticmethod
    def cloudformation_name_type() -> str:
        return "TopicName"

    @staticmethod
    def cloudformation_type() -> str:
        # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-sns-topic.html
        return "AWS::SNS::Topic"

    @classmethod
    def create_from_cloudformation_json(  # type: ignore[misc]
        cls,
        resource_name: str,
        cloudformation_json: Any,
        account_id: str,
        region_name: str,
        **kwargs: Any,
    ) -> "Topic":
        sns_backend = sns_backends[account_id][region_name]
        properties = cloudformation_json["Properties"]

        topic = sns_backend.create_topic(resource_name)
        for subscription in properties.get("Subscription", []):
            sns_backend.subscribe(
                topic.arn, subscription["Endpoint"], subscription["Protocol"]
            )
        return topic

    @classmethod
    def update_from_cloudformation_json(  # type: ignore[misc]
        cls,
        original_resource: Any,
        new_resource_name: str,
        cloudformation_json: Any,
        account_id: str,
        region_name: str,
    ) -> "Topic":
        cls.delete_from_cloudformation_json(
            original_resource.name, cloudformation_json, account_id, region_name
        )
        return cls.create_from_cloudformation_json(
            new_resource_name, cloudformation_json, account_id, region_name
        )

    @classmethod
    def delete_from_cloudformation_json(  # type: ignore[misc]
        cls,
        resource_name: str,
        cloudformation_json: Any,
        account_id: str,
        region_name: str,
    ) -> None:
        sns_backend = sns_backends[account_id][region_name]
        properties = cloudformation_json["Properties"]

        topic_name = properties.get(cls.cloudformation_name_type()) or resource_name
        topic_arn = make_arn_for_topic(account_id, topic_name, sns_backend.region_name)
        subscriptions, _ = sns_backend.list_subscriptions(topic_arn)
        for subscription in subscriptions:
            sns_backend.unsubscribe(subscription.arn)
        sns_backend.delete_topic(topic_arn)

    def _create_default_topic_policy(
        self, region_name: str, account_id: str, name: str
    ) -> Dict[str, Any]:
        return {
            "Version": "2008-10-17",
            "Id": "__default_policy_ID",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Sid": "__default_statement_ID",
                    "Principal": {"AWS": "*"},
                    "Action": [
                        "SNS:GetTopicAttributes",
                        "SNS:SetTopicAttributes",
                        "SNS:AddPermission",
                        "SNS:RemovePermission",
                        "SNS:DeleteTopic",
                        "SNS:Subscribe",
                        "SNS:ListSubscriptionsByTopic",
                        "SNS:Publish",
                        "SNS:Receive",
                    ],
                    "Resource": make_arn_for_topic(self.account_id, name, region_name),
                    "Condition": {"StringEquals": {"AWS:SourceOwner": str(account_id)}},
                }
            ],
        }


class Subscription(BaseModel):
    def __init__(self, account_id: str, topic: Topic, endpoint: str, protocol: str):
        self.account_id = account_id
        self.topic = topic
        self.endpoint = endpoint
        self.protocol = protocol
        self.arn = make_arn_for_subscription(self.topic.arn)
        self.attributes: Dict[str, Any] = {}
        self._filter_policy = None  # filter policy as a dict, not json.
        self.confirmed = False

    def publish(
        self,
        message: str,
        message_id: str,
        subject: Optional[str] = None,
        message_attributes: Optional[Dict[str, Any]] = None,
        group_id: Optional[str] = None,
        deduplication_id: Optional[str] = None,
    ) -> None:
        if not self._matches_filter_policy(message_attributes):
            return

        if self.protocol == "sqs":
            queue_name = self.endpoint.split(":")[-1]
            region = self.endpoint.split(":")[3]
            if self.attributes.get("RawMessageDelivery") != "true":
                sqs_backends[self.account_id][region].send_message(
                    queue_name,
                    json.dumps(
                        self.get_post_data(
                            message,
                            message_id,
                            subject,
                            message_attributes=message_attributes,
                        ),
                        sort_keys=True,
                        indent=2,
                        separators=(",", ": "),
                    ),
                    deduplication_id=deduplication_id,
                    group_id=group_id,
                )
            else:
                raw_message_attributes = {}
                for key, value in message_attributes.items():  # type: ignore
                    attr_type = "string_value"
                    type_value = value["Value"]
                    if value["Type"].startswith("Binary"):
                        attr_type = "binary_value"
                    elif value["Type"].startswith("Number"):
                        type_value = str(value["Value"])

                    raw_message_attributes[key] = {
                        "data_type": value["Type"],
                        attr_type: type_value,
                    }

                sqs_backends[self.account_id][region].send_message(
                    queue_name,
                    message,
                    message_attributes=raw_message_attributes,
                    deduplication_id=deduplication_id,
                    group_id=group_id,
                )
        elif self.protocol in ["http", "https"]:
            post_data = self.get_post_data(message, message_id, subject)
            requests.post(
                self.endpoint,
                json=post_data,
                headers={"Content-Type": "text/plain; charset=UTF-8"},
            )
        elif self.protocol == "lambda":
            # TODO: support bad function name
            # http://docs.aws.amazon.com/general/latest/gr/aws-arns-and-namespaces.html
            arr = self.endpoint.split(":")
            region = arr[3]
            qualifier = None
            if len(arr) == 7:
                assert arr[5] == "function"
                function_name = arr[-1]
            elif len(arr) == 8:
                assert arr[5] == "function"
                qualifier = arr[-1]
                function_name = arr[-2]
            else:
                assert False

            from moto.awslambda import lambda_backends

            lambda_backends[self.account_id][region].send_sns_message(
                function_name, message, subject=subject, qualifier=qualifier
            )

    def _matches_filter_policy(
        self, message_attributes: Optional[Dict[str, Any]]
    ) -> bool:
        if not self._filter_policy:
            return True

        if message_attributes is None:
            message_attributes = {}

        def _field_match(
            field: str, rules: List[Any], message_attributes: Dict[str, Any]
        ) -> bool:
            for rule in rules:
                #  TODO: boolean value matching is not supported, SNS behavior unknown
                if isinstance(rule, str):
                    if field not in message_attributes:
                        return False
                    if message_attributes[field]["Value"] == rule:
                        return True
                    try:
                        json_data = json.loads(message_attributes[field]["Value"])
                        if rule in json_data:
                            return True
                    except (ValueError, TypeError):
                        pass
                if isinstance(rule, (int, float)):
                    if field not in message_attributes:
                        return False
                    if message_attributes[field]["Type"] == "Number":
                        attribute_values = [message_attributes[field]["Value"]]
                    elif message_attributes[field]["Type"] == "String.Array":
                        try:
                            attribute_values = json.loads(
                                message_attributes[field]["Value"]
                            )
                            if not isinstance(attribute_values, list):
                                attribute_values = [attribute_values]
                        except (ValueError, TypeError):
                            return False
                    else:
                        return False

                    for attribute_value in attribute_values:
                        attribute_value = float(attribute_value)
                        # Even the official documentation states a 5 digits of accuracy after the decimal point for numerics, in reality it is 6
                        # https://docs.aws.amazon.com/sns/latest/dg/sns-subscription-filter-policies.html#subscription-filter-policy-constraints
                        if int(attribute_value * 1000000) == int(rule * 1000000):
                            return True
                if isinstance(rule, dict):
                    keyword = list(rule.keys())[0]
                    value = list(rule.values())[0]
                    if keyword == "exists":
                        if value and field in message_attributes:
                            return True
                        elif not value and field not in message_attributes:
                            return True
                    elif keyword == "prefix" and isinstance(value, str):
                        if field in message_attributes:
                            attr = message_attributes[field]
                            if attr["Type"] == "String" and attr["Value"].startswith(
                                value
                            ):
                                return True
                    elif keyword == "anything-but":
                        if field not in message_attributes:
                            continue
                        attr = message_attributes[field]
                        if isinstance(value, dict):
                            # We can combine anything-but with the prefix-filter
                            anything_but_key = list(value.keys())[0]
                            anything_but_val = list(value.values())[0]
                            if anything_but_key != "prefix":
                                return False
                            if attr["Type"] == "String":
                                actual_values = [attr["Value"]]
                            else:
                                actual_values = [v for v in attr["Value"]]
                            if all(
                                [
                                    not v.startswith(anything_but_val)
                                    for v in actual_values
                                ]
                            ):
                                return True
                        else:
                            undesired_values = (
                                [value] if isinstance(value, str) else value
                            )
                            if attr["Type"] == "Number":
                                actual_values = [float(attr["Value"])]
                            elif attr["Type"] == "String":
                                actual_values = [attr["Value"]]
                            else:
                                actual_values = [v for v in attr["Value"]]
                            if all([v not in undesired_values for v in actual_values]):
                                return True
                    elif keyword == "numeric" and isinstance(value, list):
                        # [(< x), (=, y), (>=, z)]
                        numeric_ranges = zip(value[0::2], value[1::2])
                        if (
                            message_attributes.get(field, {}).get("Type", "")
                            == "Number"
                        ):
                            msg_value = float(message_attributes[field]["Value"])
                            matches = []
                            for operator, test_value in numeric_ranges:
                                test_value = test_value
                                if operator == ">":
                                    matches.append((msg_value > test_value))
                                if operator == ">=":
                                    matches.append((msg_value >= test_value))
                                if operator == "=":
                                    matches.append((msg_value == test_value))
                                if operator == "<":
                                    matches.append((msg_value < test_value))
                                if operator == "<=":
                                    matches.append((msg_value <= test_value))
                            return all(matches)
            return False

        return all(
            _field_match(field, rules, message_attributes)
            for field, rules in self._filter_policy.items()
        )

    def get_post_data(
        self,
        message: str,
        message_id: str,
        subject: Optional[str],
        message_attributes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        post_data: Dict[str, Any] = {
            "Type": "Notification",
            "MessageId": message_id,
            "TopicArn": self.topic.arn,
            "Message": message,
            "Timestamp": iso_8601_datetime_with_milliseconds(
                datetime.datetime.utcnow()
            ),
            "SignatureVersion": "1",
            "Signature": "EXAMPLElDMXvB8r9R83tGoNn0ecwd5UjllzsvSvbItzfaMpN2nk5HVSw7XnOn/49IkxDKz8YrlH2qJXj2iZB0Zo2O71c4qQk1fMUDi3LGpij7RCW7AW9vYYsSqIKRnFS94ilu7NFhUzLiieYr4BKHpdTmdD6c0esKEYBpabxDSc=",
            "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-f3ecfb7224c7233fe7bb5f59f96de52f.pem",
            "UnsubscribeURL": f"https://sns.us-east-1.amazonaws.com/?Action=Unsubscribe&SubscriptionArn=arn:aws:sns:us-east-1:{self.account_id}:some-topic:2bcfbf39-05c3-41de-beaa-fcfcc21c8f55",
        }
        if subject:
            post_data["Subject"] = subject
        if message_attributes:
            post_data["MessageAttributes"] = message_attributes
        return post_data


class PlatformApplication(BaseModel):
    def __init__(
        self,
        account_id: str,
        region: str,
        name: str,
        platform: str,
        attributes: Dict[str, str],
    ):
        self.region = region
        self.name = name
        self.platform = platform
        self.attributes = attributes
        self.arn = f"arn:aws:sns:{region}:{account_id}:app/{platform}/{name}"


class PlatformEndpoint(BaseModel):
    def __init__(
        self,
        account_id: str,
        region: str,
        application: PlatformApplication,
        custom_user_data: str,
        token: str,
        attributes: Dict[str, str],
    ):
        self.region = region
        self.application = application
        self.custom_user_data = custom_user_data
        self.token = token
        self.attributes = attributes
        self.id = mock_random.uuid4()
        self.arn = f"arn:aws:sns:{region}:{account_id}:endpoint/{self.application.platform}/{self.application.name}/{self.id}"
        self.messages: Dict[str, str] = OrderedDict()
        self.__fixup_attributes()

    def __fixup_attributes(self) -> None:
        # When AWS returns the attributes dict, it always contains these two elements, so we need to
        # automatically ensure they exist as well.
        if "Token" not in self.attributes:
            self.attributes["Token"] = self.token
        if "Enabled" in self.attributes:
            enabled = self.attributes["Enabled"]
            self.attributes["Enabled"] = enabled.lower()
        else:
            self.attributes["Enabled"] = "true"

    @property
    def enabled(self) -> bool:
        return json.loads(self.attributes.get("Enabled", "true").lower())

    def publish(self, message: str) -> str:
        if not self.enabled:
            raise SnsEndpointDisabled(f"Endpoint {self.id} disabled")

        # This is where we would actually send a message
        message_id = str(mock_random.uuid4())
        self.messages[message_id] = message
        return message_id


class SNSBackend(BaseBackend):
    """
    Responsible for mocking calls to SNS. Integration with SQS/HTTP/etc is supported.

    Messages published to a topic are persisted in the backend. If you need to verify that a message was published successfully, you can use the internal API to check the message was published successfully:

    .. sourcecode:: python

        from moto.core import DEFAULT_ACCOUNT_ID
        from moto.sns import sns_backends
        sns_backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]  # Use the appropriate account/region
        all_send_notifications = sns_backend.topics[topic_arn].sent_notifications

    Note that, as this is an internal API, the exact format may differ per versions.
    """

    def __init__(self, region_name: str, account_id: str):
        super().__init__(region_name, account_id)
        self.topics: Dict[str, Topic] = OrderedDict()
        self.subscriptions: OrderedDict[str, Subscription] = OrderedDict()
        self.applications: Dict[str, PlatformApplication] = {}
        self.platform_endpoints: Dict[str, PlatformEndpoint] = {}
        self.region_name = region_name
        self.sms_attributes: Dict[str, str] = {}
        self.sms_messages: Dict[str, Tuple[str, str]] = OrderedDict()
        self.opt_out_numbers = [
            "+447420500600",
            "+447420505401",
            "+447632960543",
            "+447632960028",
            "+447700900149",
            "+447700900550",
            "+447700900545",
            "+447700900907",
        ]

    @staticmethod
    def default_vpc_endpoint_service(
        service_region: str, zones: List[str]
    ) -> List[Dict[str, str]]:
        """List of dicts representing default VPC endpoints for this service."""
        return BaseBackend.default_vpc_endpoint_service_factory(
            service_region, zones, "sns"
        )

    def update_sms_attributes(self, attrs: Dict[str, str]) -> None:
        self.sms_attributes.update(attrs)

    def create_topic(
        self,
        name: str,
        attributes: Optional[Dict[str, str]] = None,
        tags: Optional[Dict[str, str]] = None,
    ) -> Topic:

        if attributes is None:
            attributes = {}
        if attributes.get("FifoTopic") and attributes["FifoTopic"].lower() == "true":
            fails_constraints = not re.match(r"^[a-zA-Z0-9_-]{1,256}\.fifo$", name)
            msg = "Fifo Topic names must end with .fifo and must be made up of only uppercase and lowercase ASCII letters, numbers, underscores, and hyphens, and must be between 1 and 256 characters long."

        else:
            fails_constraints = not re.match(r"^[a-zA-Z0-9_-]{1,256}$", name)
            msg = "Topic names must be made up of only uppercase and lowercase ASCII letters, numbers, underscores, and hyphens, and must be between 1 and 256 characters long."

        if fails_constraints:
            raise InvalidParameterValue(msg)

        candidate_topic = Topic(name, self)
        if attributes:
            for attribute in attributes:
                setattr(
                    candidate_topic,
                    camelcase_to_underscores(attribute),
                    attributes[attribute],
                )
        if tags:
            candidate_topic._tags = tags
        if candidate_topic.arn in self.topics:
            return self.topics[candidate_topic.arn]
        else:
            self.topics[candidate_topic.arn] = candidate_topic
            return candidate_topic

    def _get_values_nexttoken(
        self, values_map: Dict[str, Any], next_token: Optional[str] = None
    ) -> Tuple[List[Any], Optional[int]]:
        i_next_token = int(next_token or "0")
        values = list(values_map.values())[
            i_next_token : i_next_token + DEFAULT_PAGE_SIZE
        ]
        if len(values) == DEFAULT_PAGE_SIZE:
            i_next_token = i_next_token + DEFAULT_PAGE_SIZE
        else:
            i_next_token = None  # type: ignore
        return values, i_next_token

    def _get_topic_subscriptions(self, topic: Topic) -> List[Subscription]:
        return [sub for sub in self.subscriptions.values() if sub.topic == topic]

    def list_topics(
        self, next_token: Optional[str] = None
    ) -> Tuple[List[Topic], Optional[int]]:
        return self._get_values_nexttoken(self.topics, next_token)

    def delete_topic_subscriptions(self, topic: Topic) -> None:
        for key, value in dict(self.subscriptions).items():
            if value.topic == topic:
                self.subscriptions.pop(key)

    def delete_topic(self, arn: str) -> None:
        try:
            topic = self.get_topic(arn)
            self.delete_topic_subscriptions(topic)
            self.topics.pop(arn)
        except KeyError:
            raise SNSNotFoundError(f"Topic with arn {arn} not found")

    def get_topic(self, arn: str) -> Topic:
        try:
            return self.topics[arn]
        except KeyError:
            raise SNSNotFoundError(f"Topic with arn {arn} not found")

    def set_topic_attribute(
        self, topic_arn: str, attribute_name: str, attribute_value: str
    ) -> None:
        topic = self.get_topic(topic_arn)
        setattr(topic, attribute_name, attribute_value)

    def subscribe(self, topic_arn: str, endpoint: str, protocol: str) -> Subscription:
        if protocol == "sms":
            if re.search(r"[./-]{2,}", endpoint) or re.search(
                r"(^[./-]|[./-]$)", endpoint
            ):
                raise SNSInvalidParameter(f"Invalid SMS endpoint: {endpoint}")

            reduced_endpoint = re.sub(r"[./-]", "", endpoint)

            if not is_e164(reduced_endpoint):
                raise SNSInvalidParameter(f"Invalid SMS endpoint: {endpoint}")

        # AWS doesn't create duplicates
        old_subscription = self._find_subscription(topic_arn, endpoint, protocol)
        if old_subscription:
            return old_subscription
        topic = self.get_topic(topic_arn)
        subscription = Subscription(self.account_id, topic, endpoint, protocol)
        attributes = {
            "PendingConfirmation": "false",
            "ConfirmationWasAuthenticated": "true",
            "Endpoint": endpoint,
            "TopicArn": topic_arn,
            "Protocol": protocol,
            "SubscriptionArn": subscription.arn,
            "Owner": self.account_id,
            "RawMessageDelivery": "false",
        }

        if protocol in ["http", "https"]:
            attributes["EffectiveDeliveryPolicy"] = topic.effective_delivery_policy

        subscription.attributes = attributes
        self.subscriptions[subscription.arn] = subscription
        return subscription

    def _find_subscription(
        self, topic_arn: str, endpoint: str, protocol: str
    ) -> Optional[Subscription]:
        for subscription in self.subscriptions.values():
            if (
                subscription.topic.arn == topic_arn
                and subscription.endpoint == endpoint
                and subscription.protocol == protocol
            ):
                return subscription
        return None

    def unsubscribe(self, subscription_arn: str) -> None:
        self.subscriptions.pop(subscription_arn, None)

    def list_subscriptions(
        self, topic_arn: Optional[str] = None, next_token: Optional[str] = None
    ) -> Tuple[List[Subscription], Optional[int]]:
        if topic_arn:
            topic = self.get_topic(topic_arn)
            filtered = OrderedDict(
                [(sub.arn, sub) for sub in self._get_topic_subscriptions(topic)]
            )
            return self._get_values_nexttoken(filtered, next_token)
        else:
            return self._get_values_nexttoken(self.subscriptions, next_token)

    def publish(
        self,
        message: str,
        arn: Optional[str],
        phone_number: Optional[str] = None,
        subject: Optional[str] = None,
        message_attributes: Optional[Dict[str, Any]] = None,
        group_id: Optional[str] = None,
        deduplication_id: Optional[str] = None,
    ) -> str:
        if subject is not None and len(subject) > 100:
            # Note that the AWS docs around length are wrong: https://github.com/getmoto/moto/issues/1503
            raise ValueError("Subject must be less than 100 characters")

        if phone_number:
            # This is only an approximation. In fact, we should try to use GSM-7 or UCS-2 encoding to count used bytes
            if len(message) > MAXIMUM_SMS_MESSAGE_BYTES:
                raise ValueError("SMS message must be less than 1600 bytes")

            message_id = str(mock_random.uuid4())
            self.sms_messages[message_id] = (phone_number, message)
            return message_id

        if len(message) > MAXIMUM_MESSAGE_LENGTH:
            raise InvalidParameterValue(
                "An error occurred (InvalidParameter) when calling the Publish operation: Invalid parameter: Message too long"
            )

        try:
            topic = self.get_topic(arn)  # type: ignore

            fifo_topic = topic.fifo_topic == "true"
            if fifo_topic:
                if not group_id:
                    # MessageGroupId is a mandatory parameter for all
                    # messages in a fifo queue
                    raise MissingParameter("MessageGroupId")
                deduplication_id_required = topic.content_based_deduplication == "false"
                if not deduplication_id and deduplication_id_required:
                    raise InvalidParameterValue(
                        "The topic should either have ContentBasedDeduplication enabled or MessageDeduplicationId provided explicitly"
                    )
            elif group_id or deduplication_id:
                parameter = "MessageGroupId" if group_id else "MessageDeduplicationId"
                raise InvalidParameterValue(
                    f"Invalid parameter: {parameter} "
                    f"Reason: The request includes {parameter} parameter that is not valid for this topic type"
                )

            message_id = topic.publish(
                message,
                subject=subject,
                message_attributes=message_attributes,
                group_id=group_id,
                deduplication_id=deduplication_id,
            )
        except SNSNotFoundError:
            endpoint = self.get_endpoint(arn)  # type: ignore
            message_id = endpoint.publish(message)
        return message_id

    def create_platform_application(
        self, name: str, platform: str, attributes: Dict[str, str]
    ) -> PlatformApplication:
        application = PlatformApplication(
            self.account_id, self.region_name, name, platform, attributes
        )
        self.applications[application.arn] = application
        return application

    def get_application(self, arn: str) -> PlatformApplication:
        try:
            return self.applications[arn]
        except KeyError:
            raise SNSNotFoundError(f"Application with arn {arn} not found")

    def set_application_attributes(
        self, arn: str, attributes: Dict[str, Any]
    ) -> PlatformApplication:
        application = self.get_application(arn)
        application.attributes.update(attributes)
        return application

    def list_platform_applications(self) -> Iterable[PlatformApplication]:
        return self.applications.values()

    def delete_platform_application(self, platform_arn: str) -> None:
        self.applications.pop(platform_arn)
        endpoints = self.list_endpoints_by_platform_application(platform_arn)
        for endpoint in endpoints:
            self.platform_endpoints.pop(endpoint.arn)

    def create_platform_endpoint(
        self,
        application: PlatformApplication,
        custom_user_data: str,
        token: str,
        attributes: Dict[str, str],
    ) -> PlatformEndpoint:
        for endpoint in self.platform_endpoints.values():
            if token == endpoint.token:
                if (
                    attributes.get("Enabled", "").lower()
                    == endpoint.attributes["Enabled"]
                ):
                    return endpoint
                raise DuplicateSnsEndpointError(
                    f"Duplicate endpoint token with different attributes: {token}"
                )
        platform_endpoint = PlatformEndpoint(
            self.account_id,
            self.region_name,
            application,
            custom_user_data,
            token,
            attributes,
        )
        self.platform_endpoints[platform_endpoint.arn] = platform_endpoint
        return platform_endpoint

    def list_endpoints_by_platform_application(
        self, application_arn: str
    ) -> List[PlatformEndpoint]:
        return [
            endpoint
            for endpoint in self.platform_endpoints.values()
            if endpoint.application.arn == application_arn
        ]

    def get_endpoint(self, arn: str) -> PlatformEndpoint:
        try:
            return self.platform_endpoints[arn]
        except KeyError:
            raise SNSNotFoundError("Endpoint does not exist")

    def set_endpoint_attributes(
        self, arn: str, attributes: Dict[str, Any]
    ) -> PlatformEndpoint:
        endpoint = self.get_endpoint(arn)
        if "Enabled" in attributes:
            attributes["Enabled"] = attributes["Enabled"].lower()
        endpoint.attributes.update(attributes)
        return endpoint

    def delete_endpoint(self, arn: str) -> None:
        try:
            del self.platform_endpoints[arn]
        except KeyError:
            raise SNSNotFoundError(f"Endpoint with arn {arn} not found")

    def get_subscription_attributes(self, arn: str) -> Dict[str, Any]:
        subscription = self.subscriptions.get(arn)

        if not subscription:
            raise SNSNotFoundError(
                "Subscription does not exist", template="wrapped_single_error"
            )

        return subscription.attributes

    def set_subscription_attributes(self, arn: str, name: str, value: Any) -> None:
        if name not in [
            "RawMessageDelivery",
            "DeliveryPolicy",
            "FilterPolicy",
            "RedrivePolicy",
            "SubscriptionRoleArn",
        ]:
            raise SNSInvalidParameter("AttributeName")

        # TODO: should do validation
        _subscription = [_ for _ in self.subscriptions.values() if _.arn == arn]
        if not _subscription:
            raise SNSNotFoundError(f"Subscription with arn {arn} not found")
        subscription = _subscription[0]

        subscription.attributes[name] = value

        if name == "FilterPolicy":
            filter_policy = json.loads(value)
            self._validate_filter_policy(filter_policy)
            subscription._filter_policy = filter_policy

    def _validate_filter_policy(self, value: Any) -> None:
        # TODO: extend validation checks
        combinations = 1
        for rules in value.values():
            combinations *= len(rules)
        # Even the official documentation states the total combination of values must not exceed 100, in reality it is 150
        # https://docs.aws.amazon.com/sns/latest/dg/sns-subscription-filter-policies.html#subscription-filter-policy-constraints
        if combinations > 150:
            raise SNSInvalidParameter(
                "Invalid parameter: FilterPolicy: Filter policy is too complex"
            )

        for rules in value.values():
            for rule in rules:
                if rule is None:
                    continue
                if isinstance(rule, str):
                    continue
                if isinstance(rule, bool):
                    continue
                if isinstance(rule, (int, float)):
                    if rule <= -1000000000 or rule >= 1000000000:
                        raise InternalError("Unknown")
                    continue
                if isinstance(rule, dict):
                    keyword = list(rule.keys())[0]
                    attributes = list(rule.values())[0]
                    if keyword == "anything-but":
                        continue
                    elif keyword == "exists":
                        if not isinstance(attributes, bool):
                            raise SNSInvalidParameter(
                                "Invalid parameter: FilterPolicy: exists match pattern must be either true or false."
                            )
                        continue
                    elif keyword == "numeric":
                        # TODO: All of the exceptions listed below contain column pointing where the error is (in AWS response)
                        # Example: 'Value of < must be numeric\n at [Source: (String)"{"price":[{"numeric":["<","100"]}]}"; line: 1, column: 28]'
                        # While it probably can be implemented, it doesn't feel as important as the general parameter checking

                        attributes_copy = attributes[:]
                        if not attributes_copy:
                            raise SNSInvalidParameter(
                                "Invalid parameter: Attributes Reason: FilterPolicy: Invalid member in numeric match: ]\n at ..."
                            )

                        operator = attributes_copy.pop(0)

                        if not isinstance(operator, str):
                            raise SNSInvalidParameter(
                                f"Invalid parameter: Attributes Reason: FilterPolicy: Invalid member in numeric match: {(str(operator))}\n at ..."
                            )

                        if operator not in ("<", "<=", "=", ">", ">="):
                            raise SNSInvalidParameter(
                                f"Invalid parameter: Attributes Reason: FilterPolicy: Unrecognized numeric range operator: {(str(operator))}\n at ..."
                            )

                        try:
                            value = attributes_copy.pop(0)
                        except IndexError:
                            value = None

                        if value is None or not isinstance(value, (int, float)):
                            raise SNSInvalidParameter(
                                f"Invalid parameter: Attributes Reason: FilterPolicy: Value of {(str(operator))} must be numeric\n at ..."
                            )

                        if not attributes_copy:
                            continue

                        if operator not in (">", ">="):
                            raise SNSInvalidParameter(
                                "Invalid parameter: Attributes Reason: FilterPolicy: Too many elements in numeric expression\n at ..."
                            )

                        second_operator = attributes_copy.pop(0)

                        if second_operator not in ("<", "<="):
                            raise SNSInvalidParameter(
                                f"Invalid parameter: Attributes Reason: FilterPolicy: Bad numeric range operator: {(str(second_operator))}\n at ..."
                            )

                        try:
                            second_value = attributes_copy.pop(0)
                        except IndexError:
                            second_value = None

                        if second_value is None or not isinstance(
                            second_value, (int, float)
                        ):
                            raise SNSInvalidParameter(
                                f"Invalid parameter: Attributes Reason: FilterPolicy: Value of {(str(second_operator))} must be numeric\n at ..."
                            )

                        if second_value <= value:
                            raise SNSInvalidParameter(
                                "Invalid parameter: Attributes Reason: FilterPolicy: Bottom must be less than top\n at ..."
                            )

                        continue
                    elif keyword == "prefix":
                        continue
                    else:
                        raise SNSInvalidParameter(
                            f"Invalid parameter: FilterPolicy: Unrecognized match type {keyword}"
                        )

                raise SNSInvalidParameter(
                    "Invalid parameter: FilterPolicy: Match value must be String, number, true, false, or null"
                )

    def add_permission(
        self,
        topic_arn: str,
        label: str,
        aws_account_ids: List[str],
        action_names: List[str],
    ) -> None:
        if topic_arn not in self.topics:
            raise SNSNotFoundError("Topic does not exist")

        policy = self.topics[topic_arn]._policy_json
        statement = next(
            (
                statement
                for statement in policy["Statement"]
                if statement["Sid"] == label
            ),
            None,
        )

        if statement:
            raise SNSInvalidParameter("Statement already exists")

        if any(action_name not in VALID_POLICY_ACTIONS for action_name in action_names):
            raise SNSInvalidParameter("Policy statement action out of service scope!")

        principals = [
            f"arn:aws:iam::{account_id}:root" for account_id in aws_account_ids
        ]
        actions = [f"SNS:{action_name}" for action_name in action_names]

        statement = {
            "Sid": label,
            "Effect": "Allow",
            "Principal": {"AWS": principals[0] if len(principals) == 1 else principals},
            "Action": actions[0] if len(actions) == 1 else actions,
            "Resource": topic_arn,
        }

        self.topics[topic_arn]._policy_json["Statement"].append(statement)

    def remove_permission(self, topic_arn: str, label: str) -> None:
        if topic_arn not in self.topics:
            raise SNSNotFoundError("Topic does not exist")

        statements = self.topics[topic_arn]._policy_json["Statement"]
        statements = [
            statement for statement in statements if statement["Sid"] != label
        ]

        self.topics[topic_arn]._policy_json["Statement"] = statements

    def list_tags_for_resource(self, resource_arn: str) -> Dict[str, str]:
        if resource_arn not in self.topics:
            raise ResourceNotFoundError

        return self.topics[resource_arn]._tags

    def tag_resource(self, resource_arn: str, tags: Dict[str, str]) -> None:
        if resource_arn not in self.topics:
            raise ResourceNotFoundError

        updated_tags = self.topics[resource_arn]._tags.copy()
        updated_tags.update(tags)

        if len(updated_tags) > 50:
            raise TagLimitExceededError

        self.topics[resource_arn]._tags = updated_tags

    def untag_resource(self, resource_arn: str, tag_keys: List[str]) -> None:
        if resource_arn not in self.topics:
            raise ResourceNotFoundError

        for key in tag_keys:
            self.topics[resource_arn]._tags.pop(key, None)

    def publish_batch(
        self, topic_arn: str, publish_batch_request_entries: List[Dict[str, Any]]
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
        """
        The MessageStructure and MessageDeduplicationId-parameters have not yet been implemented.
        """
        try:
            topic = self.get_topic(topic_arn)
        except SNSNotFoundError:
            raise TopicNotFound

        if len(publish_batch_request_entries) > 10:
            raise TooManyEntriesInBatchRequest

        ids = [m["Id"] for m in publish_batch_request_entries]
        if len(set(ids)) != len(ids):
            raise BatchEntryIdsNotDistinct

        fifo_topic = topic.fifo_topic == "true"
        if fifo_topic:
            if not all(
                ["MessageGroupId" in entry for entry in publish_batch_request_entries]
            ):
                raise SNSInvalidParameter(
                    "Invalid parameter: The MessageGroupId parameter is required for FIFO topics"
                )

        successful: List[Dict[str, str]] = []
        failed: List[Dict[str, Any]] = []

        for entry in publish_batch_request_entries:
            try:
                message_id = self.publish(
                    message=entry["Message"],
                    arn=topic_arn,
                    subject=entry.get("Subject"),
                    message_attributes=entry.get("MessageAttributes", {}),
                    group_id=entry.get("MessageGroupId"),
                )
                successful.append({"MessageId": message_id, "Id": entry["Id"]})
            except Exception as e:
                if isinstance(e, InvalidParameterValue):
                    failed.append(
                        {
                            "Id": entry["Id"],
                            "Code": "InvalidParameter",
                            "Message": e.message,
                            "SenderFault": True,
                        }
                    )
        return successful, failed


sns_backends = BackendDict(SNSBackend, "sns")


DEFAULT_EFFECTIVE_DELIVERY_POLICY = {
    "defaultHealthyRetryPolicy": {
        "numNoDelayRetries": 0,
        "numMinDelayRetries": 0,
        "minDelayTarget": 20,
        "maxDelayTarget": 20,
        "numMaxDelayRetries": 0,
        "numRetries": 3,
        "backoffFunction": "linear",
    },
    "sicklyRetryPolicy": None,
    "throttlePolicy": None,
    "guaranteed": False,
}


VALID_POLICY_ACTIONS = [
    "GetTopicAttributes",
    "SetTopicAttributes",
    "AddPermission",
    "RemovePermission",
    "DeleteTopic",
    "Subscribe",
    "ListSubscriptionsByTopic",
    "Publish",
    "Receive",
]