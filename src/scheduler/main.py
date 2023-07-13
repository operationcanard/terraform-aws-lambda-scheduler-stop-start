"""This script stop and start aws resources."""

import os
import logging
from distutils.util import strtobool

from .autoscaling.handler import AutoscalingScheduler
from .cloudwatch.handler import CloudWatchAlarmScheduler
from .documentdb.handler import DocumentDBScheduler
from .ec2.handler import InstanceScheduler
from .ecs.handler import EcsScheduler
from .rds.handler import RdsScheduler
from .redshift.handler import RedshiftScheduler


def lambda_handler(event, context):
    """Main function entrypoint for lambda.

    Stop and start AWS resources:
    - rds instances
    - rds aurora clusters
    - instance ec2
    - ecs services

    Suspend and resume AWS resources:
    - ec2 autoscaling groups

    Terminate spot instances (spot instance cannot be stopped by a user)
    """

    # Retrieve variables from aws lambda ENVIRONMENT
    schedule_action = os.getenv("SCHEDULE_ACTION")
    aws_regions = os.getenv("AWS_REGIONS").replace(" ", "").split(",")
    format_tags = [{"Key": os.getenv("TAG_KEY"), "Values": [os.getenv("TAG_VALUE")]}]

    autoscaling_terminate_instances = strtobool(
        os.getenv("AUTOSCALING_TERMINATE_INSTANCES", "false")
    )

    _strategy = {
        InstanceScheduler: os.getenv("EC2_SCHEDULE", "false"),
        EcsScheduler: os.getenv("ECS_SCHEDULE", "false"),
        RdsScheduler: os.getenv("RDS_SCHEDULE", "false"),
        AutoscalingScheduler: os.getenv("AUTOSCALING_SCHEDULE", "false"),
        DocumentDBScheduler: os.getenv("DOCUMENTDB_SCHEDULE", "false"),
        RedshiftScheduler: os.getenv("REDSHIFT_SCHEDULE", "false"),
        CloudWatchAlarmScheduler: os.getenv("CLOUDWATCH_ALARM_SCHEDULE", "false"),
    }

    print(f"[LOG] STRATEGY ENV LOADED: {_strategy}")
    print(f"[LOG] AWS-REGION ENV LOADED: {aws_regions}")
    print(f"[LOG] FORMAT-TAGS ENV LOADED: {format_tags}")
    print(f"[LOG] ACTION ENV LOADED: {schedule_action}")

    for service, to_schedule in _strategy.items():
        if strtobool(to_schedule):
            for aws_region in aws_regions:
                logging.info(f"[LOG] Run scheduler {service} on {aws_region}")
                strategy = service(aws_region)
                if service == AutoscalingScheduler and autoscaling_terminate_instances:
                    getattr(strategy, schedule_action)(
                        aws_tags=format_tags, terminate_instances=True
                    )
                else:
                    getattr(strategy, schedule_action)(aws_tags=format_tags)
