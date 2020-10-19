import logging

import boto3
import botocore.exceptions

from . import dynamodb
from . import ec2
from . import eks
from . import elasticsearch
from . import iam
from . import lambda_function
from . import organizations
from . import permission_relationships
from . import rds
from . import redshift
from . import resourcegroupstaggingapi
from . import route53
from . import s3
from cartography.util import run_analysis_job
from cartography.util import run_cleanup_job
from cartography.util import timeit
import lib.multiprocessing_dag as multiprocessing_dag
from cartography.multisync import build_cartography_sync_task

logger = logging.getLogger(__name__)


def _sync_one_account(neo4j_session, aws_sync_config):
    try:
        aws_sync_config['regions'] = ec2.get_ec2_regions(aws_sync_config['boto3_session'])
    except botocore.exceptions.ClientError as e:
        logger.debug("Error occurred getting EC2 regions.", exc_info=True)
        logger.error(
            (
                "Failed to retrieve AWS region list, an error occurred: %s. Could not get regions for account %s."
            ),
            e,
            aws_sync_config['account_id'],
        )
        return

    pipeline = multiprocessing_dag.Pipeline(name='pipeline_aws_one_account')

    # Initialization pipeline -- all subsequent pipelines should have this upstream.
    pipeline_start = multiprocessing_dag.Pipeline(name='pipeline_aws_one_account_init')
    # TODO - adapt IAM and S3 syncs to use the region data
    for func in [iam.sync, s3.sync, dynamodb.sync, ec2.sync, eks.sync, lambda_function.sync, rds.sync, redshift.sync]:
        task = build_cartography_sync_task(aws_sync_config, func.__name__, func)
        pipeline_start.add(task)
    pipeline.add(pipeline_start)

    # NOTE each of the below will generate DNS records
    route53.sync(neo4j_session, aws_sync_config)
    elasticsearch.sync(neo4j_session, aws_sync_config)

    # TODO put this in a stage somehow
    # NOTE clean up all DNS records, regardless of which job created them
    run_cleanup_job('aws_account_dns_cleanup.json', neo4j_session, aws_sync_config['common_job_parameters'])

    pipeline_rpr = multiprocessing_dag.Pipeline(name='pipeline_aws_one_account_rpr')
    rpr_task = build_cartography_sync_task(aws_sync_config, 'permission_relationships.sync',
                                           permission_relationships.sync)
    pipeline_rpr.add(rpr_task)
    pipeline.add(pipeline_rpr, upstreams=[pipeline_start])

    pipeline_tags = multiprocessing_dag.Pipeline(name='pipeline_aws_one_account_tags')
    tag_task = build_cartography_sync_task(aws_sync_config, 'resourcegroupstaggingapi.sync',
                                           resourcegroupstaggingapi.sync)
    pipeline_tags.add(tag_task)
    pipeline.add(pipeline_tags, upstreams=[pipeline_start])

    # TODO - do I need to add a pipeline_sequential here?
    runner = multiprocessing_dag.Runner()
    return runner.run(pipeline)


def _autodiscover_accounts(neo4j_session, boto3_session, account_id, sync_tag, common_job_parameters):
    logger.info("Trying to autodiscover accounts.")
    try:
        # Fetch all accounts
        client = boto3_session.client('organizations')
        paginator = client.get_paginator('list_accounts')
        accounts = []
        for page in paginator.paginate():
            accounts.extend(page['Accounts'])

        # Filter out every account which is not in the ACTIVE status
        # and select only the Id and Name fields
        accounts = {x['Name']: x['Id'] for x in accounts if x['Status'] == 'ACTIVE'}

        # Add them to the graph
        logger.info("Loading autodiscovered accounts.")
        organizations.load_aws_accounts(neo4j_session, accounts, sync_tag, common_job_parameters)
    except botocore.exceptions.ClientError:
        logger.debug(f"The current account ({account_id}) doesn't have enough permissions to perform autodiscovery.")


def _sync_multiple_accounts(neo4j_session, accounts, sync_tag, common_job_parameters):
    logger.debug("Syncing AWS accounts: %s", ', '.join(accounts.values()))
    organizations.sync(neo4j_session, accounts, sync_tag, common_job_parameters)

    for profile_name, account_id in accounts.items():
        logger.info("Syncing AWS account with ID '%s' using configured profile '%s'.", account_id, profile_name)
        common_job_parameters["AWS_ID"] = account_id
        boto3_session = boto3.Session(profile_name=profile_name)

        _autodiscover_accounts(neo4j_session, boto3_session, account_id, sync_tag, common_job_parameters)
        aws_sync_config = {
            'boto3_session': boto3_session,
            'account_id': account_id,
            'sync_tag': sync_tag,
            'common_job_parameters': common_job_parameters,
        }
        _sync_one_account(neo4j_session, aws_sync_config)

    del common_job_parameters["AWS_ID"]

    # There may be orphan Principals which point outside of known AWS accounts. This job cleans
    # up those nodes after all AWS accounts have been synced.
    run_cleanup_job('aws_post_ingestion_principals_cleanup.json', neo4j_session, common_job_parameters)

    # There may be orphan DNS entries that point outside of known AWS zones. This job cleans
    # up those entries after all AWS accounts have been synced.
    run_cleanup_job('aws_post_ingestion_dns_cleanup.json', neo4j_session, common_job_parameters)


@timeit
def start_aws_ingestion(neo4j_session, config):
    common_job_parameters = {
        "UPDATE_TAG": config.update_tag,
        "permission_relationships_file": config.permission_relationships_file,
    }
    try:
        boto3_session = boto3.Session()
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
        logger.debug("Error occurred calling boto3.Session().", exc_info=True)
        logger.error(
            (
                "Unable to initialize the default AWS session, an error occurred: %s. Make sure your AWS credentials "
                "are configured correctly, your AWS config file is valid, and your credentials have the SecurityAudit "
                "policy attached."
            ),
            e,
        )
        return

    if config.aws_sync_all_profiles:
        aws_accounts = organizations.get_aws_accounts_from_botocore_config(boto3_session)
    else:
        aws_accounts = organizations.get_aws_account_default(boto3_session)

    if not aws_accounts:
        logger.warning(
            "No valid AWS credentials could be found. No AWS accounts can be synced. Exiting AWS sync stage.",
        )
        return
    if len(list(aws_accounts.values())) != len(set(aws_accounts.values())):
        logger.warning(
            (
                "There are duplicate AWS accounts in your AWS configuration. It is strongly recommended that you run "
                "cartography with an AWS configuration which has exactly one profile for each AWS account you want to "
                "sync. Doing otherwise will result in undefined and untested behavior."
            ),
        )

    _sync_multiple_accounts(neo4j_session, aws_accounts, config.update_tag, common_job_parameters)

    run_analysis_job(
        'aws_ec2_asset_exposure.json',
        neo4j_session,
        common_job_parameters,
    )

    run_analysis_job(
        'aws_ec2_keypair_analysis.json',
        neo4j_session,
        common_job_parameters,
    )

    run_analysis_job(
        'aws_eks_asset_exposure.json',
        neo4j_session,
        common_job_parameters,
    )
