# -*- coding: utf-8 -*-

"""Exception function for all aws scheduler."""

import logging

from botocore.exceptions import ClientError


def cloudwatch_exception(resource_name: str, resource_id: str, exception: ClientError):
    """Exception raised during execution of Cloudwatch scheduler.

    Log Cloudwatch exceptions on the specific aws resources.

    :param str resource_name:
        Aws resource name
    :param str resource_id:
        Aws resource id
    :param str exception:
        Human-readable string describing the exception
    """
    logging.error(
        f"{resource_name} {resource_id}: {exception.response['Error']['Message']}"
    )
