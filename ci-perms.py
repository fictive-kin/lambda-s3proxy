#!/usr/bin/env python3

from awacs import (
    aws,
    awslambda,
    cloudformation,
    cloudfront,
    events,
    iam,
    kms,
    s3,
    sts
)
import click


@click.group()
def cli():
    pass


def get_policy(client, account):
    policy = aws.PolicyDocument(
        Version="2012-10-17",
        Statement=[
            aws.Statement(
                Effect=aws.Allow,
                Action=[
                    sts.GetCallerIdentity,
                ],
                Resource=[
                    "*",
                ],
            ),
            aws.Statement(
                Effect=aws.Allow,
                Action=[
                    iam.CreateRole,
                    iam.CreateServiceLinkedRole,
                    iam.GetRole,
                    iam.GetRolePolicy,
                    iam.PassRole,
                    iam.PutRolePolicy,
                ],
                Resource=[
                    iam.ARN(region="", account=account, resource=f"role/fictive-{client}-*"),
                    iam.ARN(region="", account=account, resource="role/fk-s3proxy-lambda"),
                ],
            ),
            aws.Statement(
                Effect=aws.Allow,
                Action=[
                    kms.CreateGrant,
                    kms.Decrypt,
                    kms.DescribeKey,
                    kms.Encrypt,
                ],
                Resource=[
                    kms.ARN(region="*", account=account, resource="key/*"),
                ],
            ),
            aws.Statement(
                Effect=aws.Allow,
                Action=[
                    s3.AbortMultipartUpload,
                    s3.DeleteObject,
                    s3.DeleteObjectVersion,
                    s3.GetObject,
                    s3.GetObjectAcl,
                    s3.GetObjectAttributes,
                    s3.GetObjectLegalHold,
                    s3.GetObjectRetention,
                    s3.GetObjectTagging,
                    s3.GetObjectTorrent,
                    s3.GetObjectVersion,
                    s3.GetObjectVersionAcl,
                    s3.GetObjectVersionAttributes,
                    s3.GetObjectVersionForReplication,
                    s3.GetObjectVersionTagging,
                    s3.GetObjectVersionTorrent,
                    s3.PutObject,
                    s3.PutObjectAcl,
                    s3.PutObjectRetention,
                    s3.PutObjectTagging,
                    s3.PutObjectVersionAcl,
                    s3.PutObjectVersionTagging,
                ],
                Resource=[
                    s3.ARN(region="", account=account, resource=f"fictive-{client}-*/*"),
                    s3.ARN(region="", account=account, resource=f"fictive-{client}/*"),
                ],
            ),
            aws.Statement(
                Effect=aws.Allow,
                Action=[
                    s3.DeleteBucketPolicy,
                    s3.GetBucketAcl,
                    s3.GetBucketCORS,
                    s3.GetBucketLocation,
                    s3.GetBucketLogging,
                    s3.GetBucketNotification,
                    s3.GetBucketOwnershipControls,
                    s3.GetBucketPolicy,
                    s3.GetBucketTagging,
                    s3.GetBucketVersioning,
                    s3.ListBucket,
                    s3.ListBucketMultipartUploads,
                    s3.PutBucketAcl,
                    s3.PutBucketPolicy,
                    s3.PutBucketTagging,
                ],
                Resource=[
                    s3.ARN(region="", account=account, resource=f"fictive-{client}"),
                    s3.ARN(region="", account=account, resource=f"fictive-{client}-*"),
                ],
            ),
            aws.Statement(
                Effect=aws.Allow,
                Action=[
                    s3.ListAllMyBuckets,
                ],
                Resource=[
                    s3.ARN(region="", account=account, resource="*"),
                ],
            ),
            aws.Statement(
                Effect=aws.Allow,
                Action=[
                    cloudfront.CreateInvalidation,
                    cloudfront.ListInvalidations,
                ],
                Resource=[
                    cloudfront.ARN(region="", account=account, resource="distribution/*"),
                ],
            ),
            aws.Statement(
                Effect=aws.Allow,
                Action=[
                    cloudformation.CreateStack,
                    cloudformation.DescribeStackResource,
                    cloudformation.DescribeStacks,
                    cloudformation.ListStackResources,
                ],
                Resource=[
                    cloudformation.ARN(region="*", account=account, resource="stack/s3proxy-lambda-*"),
                ],
            ),
            aws.Statement(
                Effect=aws.Allow,
                Action=[
                    events.ListRuleNamesByTarget,
                    events.PutRule,
                    events.PutTargets,
                ],
                Resource=[
                    events.ARN(region="*", account=account, resource="rule/*"),
                ],
            ),
            aws.Statement(
                Effect=aws.Allow,
                Action=[
                    awslambda.CreateFunction,
                    awslambda.DeleteFunction,
                    awslambda.GetFunction,
                    awslambda.GetFunctionConfiguration,
                    awslambda.ListAliases,
                    awslambda.ListVersionsByFunction,
                    awslambda.UpdateFunctionCode,
                    awslambda.UpdateFunctionConfiguration,
                ],
                Resource=[
                    awslambda.ARN(region="*", account=account, resource=f"function:fictive-{client}-*"),
                    awslambda.ARN(region="*", account=account, resource="function:s3proxy-lambda-*"),
                ],
            )
        ],
    )
    return policy


@cli.command()
@click.argument('client', type=str)
@click.argument('account', type=int)
def show_policy(client, account):
    """
    Display the CI policy that would be created
    """

    print(get_policy(client, account).to_json())


@cli.command()
@click.argument('name', type=str)
@click.argument('client', type=str)
@click.argument('account', type=int)
def create_policy(name, client, account):
    """
    Create the CI policy in AWS
    """

    policy = get_policy(client, account)
    iam = boto3.client('iam')
    iam.create_policy(
        PolicyName=name,
        PolicyDocument=policy.to_json(),
    )


if __name__ == "__main__":
    cli()
