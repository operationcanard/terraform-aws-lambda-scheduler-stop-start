"""ec2 instances scheduler."""
import logging
from typing import Dict, List

import boto3
from botocore.exceptions import ClientError

from ..libs.filter_resources_by_tags import FilterByTags
from .exceptions import ec2_exception


class InstanceScheduler:
    """Abstract ec2 scheduler in a class."""

    def __init__(self, region_name=None) -> None:
        """Initialize ec2 scheduler."""
        if region_name:
            self.ec2 = boto3.client("ec2", region_name=region_name)
            self.asg = boto3.client("autoscaling", region_name=region_name)
        else:
            self.ec2 = boto3.client("ec2")
            self.asg = boto3.client("autoscaling")
        self.tag_api = FilterByTags(region_name=region_name)

    def stop(self, aws_tags: list[dict], to_exclude=None) -> None:
        """Aws ec2 instance stop function.

        Stop ec2 instances with defined tags and disable its Cloudwatch
        alarms.

        :param list[map] aws_tags:
            Aws tags to use for filter resources.
            For example:
            [
                {
                    'Key': 'string',
                    'Values': [
                        'string',
                    ]
                }
            ]
        """
        if to_exclude is None:
            to_exclude = []

        for instance_arn in self.tag_api.get_resources("ec2:instance", aws_tags):
            instance_id = instance_arn.split("/")[-1]

            if instance_id in to_exclude:
                logging.info(f"{instance_id} found in exclude list.")
                continue

            try:
                if not self.asg.describe_auto_scaling_instances(
                        InstanceIds=[instance_id]
                )["AutoScalingInstances"]:
                    self.ec2.stop_instances(InstanceIds=[instance_id])
                    print(f"Stop instances {instance_id}")
            except ClientError as exc:
                ec2_exception("instance", instance_id, exc)

    def start(self, aws_tags: list[dict], to_exclude=None) -> None:
        """Aws ec2 instance start function.

        Start ec2 instances with defined tags.

        Aws tags to use for filter resources
            Aws tags to use for filter resources.
            For example:
            [
                {
                    'Key': 'string',
                    'Values': [
                        'string',
                    ]
                }
            ]
        """
        if to_exclude is None:
            to_exclude = []

        for instance_arn in self.tag_api.get_resources("ec2:instance", aws_tags):
            instance_id = instance_arn.split("/")[-1]

            if instance_id in to_exclude:
                logging.info(f"{instance_id} found in exclude list.")
                continue

            try:
                if not self.asg.describe_auto_scaling_instances(
                        InstanceIds=[instance_id]
                )["AutoScalingInstances"]:
                    self.ec2.start_instances(InstanceIds=[instance_id])
                    print(f"Start instances {instance_id}")
            except ClientError as exc:
                ec2_exception("instance", instance_id, exc)
