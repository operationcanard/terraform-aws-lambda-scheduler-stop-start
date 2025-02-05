# -*- coding: utf-8 -*-

"""Exception function for all aws scheduler."""

import logging

from botocore.exceptions import ClientError


def rds_exception(resource_name: str, resource_id: str, exception: ClientError) -> None:
    """Exception raised during execution of rds scheduler.

    Log rds exceptions on the specific aws resources.

    :param str resource_name:
        Aws resource name
    :param str resource_id:
        Aws resource id
    :param str exception:
        Human-readable string describing the exception
    """
    info_codes = ["InvalidParameterCombination", "DBClusterNotFoundFault"]
    warning_codes = ["InvalidDBClusterStateFault", "InvalidDBInstanceState"]

    if exception.response["Error"]["Code"] in info_codes:
        logging.info(
            "%s %s: %s",
            resource_name,
            resource_id,
            exception,
        )
    elif exception.response["Error"]["Code"] in warning_codes:
        logging.warning(
            "%s %s: %s",
            resource_name,
            resource_id,
            exception,
        )
    else:
        logging.error(
            "Unexpected error on %s %s: %s",
            resource_name,
            resource_id,
            exception,
        )
