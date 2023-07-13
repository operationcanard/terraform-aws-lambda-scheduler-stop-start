"""Exception function for all aws scheduler."""

import logging

from botocore.exceptions import ClientError


def redshift_exception(resource_name: str, resource_id: str, exception: ClientError) -> None:
    """Exception raised during execution of ec2 scheduler.

    Log instance, spot instance and autoscaling groups exceptions
    on the specific aws resources.

    :param str resource_name:
        Aws resource name
    :param str resource_id:
        Aws resource id
    :param str exception:
        Human-readable string describing the exception
    """
    info_codes = ["IncorrectInstanceState"]
    warning_codes = [
        "UnsupportedOperation",
        "IncorrectInstanceState",
        "InvalidParameterCombination",
    ]

    if exception.response["Error"]["Code"] in info_codes:
        logging.info(
            f"{resource_name} {resource_id}: {exception.response['Error']['Message']}"
        )
    elif exception.response["Error"]["Code"] in warning_codes:
        logging.warning(
            f"{resource_name} {resource_id}: {exception.response['Error']['Message']}"
        )
    else:
        logging.error(
            f"{resource_name} {resource_id}: {exception.response['Error']['Message']}"
        )
