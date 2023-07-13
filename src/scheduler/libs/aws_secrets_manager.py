import json

import boto3
from botocore.exceptions import ClientError

"""
Not used by maybe useful in the future.
"""
class GetExceptionSecrets:
    def __init__(self, region_name=None) -> None:
        """Initialize secretsmanager ."""
        if region_name:
            self.secret = boto3.client("secretsmanager", region_name=region_name)
        else:
            self.secret = boto3.client("secretsmanager")

    def get_secret(self, secret_name):
        try:
            get_secret_value_response = self.secret.get_secret_value(
                SecretId=secret_name
            )
        except ClientError as e:
            # For a list of exceptions thrown, see
            # https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html
            raise e

        # Decrypts secret using the associated KMS key.
        secret = get_secret_value_response["SecretString"]
        return json.loads(secret).keys()
