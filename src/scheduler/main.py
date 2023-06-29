"""This script stop and start aws resources."""
import logging
import os
from distutils.util import strtobool

import requests
import validators

from .autoscaling.handler import AutoscalingScheduler
from .cloudwatch.handler import CloudWatchAlarmScheduler
from .ec2.handler import InstanceScheduler
from .ecs.handler import EcsScheduler
from .rds.handler import RdsScheduler
from .libs.aws_secrets_manager import GetExceptionSecrets

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

    exclude_ec2_ids = []

    if os.getenv("EXCLUDE_EC2_IDS_FROM_URL", None) and validators.url(os.getenv("EXCLUDE_EC2_IDS_FROM_URL")):
        req = requests.get(url=os.getenv("EXCLUDE_EC2_IDS_FROM_URL"), timeout=5)
        if req.status_code == 200:
            to_exclude_from_url = req.json()
            exclude_ec2_ids += to_exclude_from_url
            logging.info(f"Exclude Instances ids list through file : {to_exclude_from_url}")
        else:
            logging.error(f"Invalid url response from {os.getenv('EXCLUDE_EC2_IDS_FROM_URL')}: HTTP/{req.status_code}")

    if os.getenv("EXCLUDE_EC2_IDS_STATICS", None):
        try:
            to_exclude_statics = os.getenv("EXCLUDE_EC2_IDS_STATICS").replace(" ", "").split(",")
            exclude_ec2_ids += to_exclude_statics
            logging.info(f"Exclude Instances ids list static configuration : {to_exclude_statics}")
        except Exception as err:
            logging.error(f"Invalid json answer: {err}")

    '''
    if os.getenv("EXCLUDE_EC2_IDS_FROM_SECRETS_MANAGER", None):
        try:
            exceptions_in_secrets_manager = GetExceptionSecrets(region_name="eu-west-1")
            to_exclude_secret_manager = exceptions_in_secrets_manager.get_secret(
                os.getenv("EXCLUDE_EC2_IDS_FROM_SECRETS_MANAGER", None)
            )
            exclude_ec2_ids += to_exclude_secret_manager
            logging.info(f"Exclude Instances ids list from secrets manager : {to_exclude_secret_manager}")
        except Exception as err:
            logging.error(f"Invalid json answer: {err}")
    '''

    _strategy = {}
    _strategy[AutoscalingScheduler] = os.getenv("AUTOSCALING_SCHEDULE")
    _strategy[InstanceScheduler] = os.getenv("EC2_SCHEDULE")
    _strategy[EcsScheduler] = os.getenv("ECS_SCHEDULE")
    _strategy[RdsScheduler] = os.getenv("RDS_SCHEDULE")
    _strategy[CloudWatchAlarmScheduler] = os.getenv("CLOUDWATCH_ALARM_SCHEDULE")

    for service, to_schedule in _strategy.items():
        if strtobool(to_schedule):
            for aws_region in aws_regions:
                strategy = service(aws_region)
                getattr(strategy, schedule_action)(aws_tags=format_tags, to_exclude=exclude_ec2_ids)
