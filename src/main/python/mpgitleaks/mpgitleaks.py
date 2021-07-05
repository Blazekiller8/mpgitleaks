#   -*- coding: utf-8 -*-
import os
import re
import sys
import shutil
import logging
import argparse
import subprocess
from queue import Empty
from pathlib import Path
from multiprocessing import Queue

from mp4ansi import MP4ansi
from github3api import GitHubAPI

logger = logging.getLogger(__name__)

HOME = '/opt/mpgitleaks'
MAX_PROCESSES = 35


def get_parser():
    """ return argument parser
    """
    parser = argparse.ArgumentParser(
        description='A Python script that wraps the gitleaks tool to enable scanning of multiple repositories in parallel')
    parser.add_argument(
        '--file',
        dest='filename',
        type=str,
        default='repos.txt',
        required=False,
        help='process repos contained in the specified file')
    parser.add_argument(
        '--user',
        dest='user',
        action='store_true',
        help='process repos for the authenticated user')
    parser.add_argument(
        '--org',
        dest='org',
        type=str,
        default=None,
        required=False,
        help='process repos for the specified organization')
    parser.add_argument(
        '--exclude',
        dest='exclude',
        type=str,
        default='',
        required=False,
        help='a regex to match name of repos to exclude from scanning')
    parser.add_argument(
        '--include',
        dest='include',
        type=str,
        default='',
        required=False,
        help='a regex to match name of repos to include in scanning')
    parser.add_argument(
        '--progress',
        dest='progress',
        action='store_true',
        help='display progress bar for each process')
    parser.add_argument(
        '--log',
        dest='log',
        action='store_true',
        help='log messages to a log file')
    return parser


def configure_logging(create):
    """ configure logging
    """
    if not create:
        return
    name = os.path.basename(sys.argv[0])
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    file_handler = logging.FileHandler(f'{name}.log')
    file_formatter = logging.Formatter("%(asctime)s %(processName)s [%(funcName)s] %(levelname)s %(message)s")
    file_handler.setFormatter(file_formatter)
    root_logger.addHandler(file_handler)


def echo(message):
    """ print and log message
    """
    logger.debug(message)
    print(message)


def get_client():
    """ return instance of GitHubAPI client
    """
    if not os.getenv('GH_TOKEN_PSW'):
        raise ValueError('GH_TOKEN_PSW environment variable must be set to token')
    return GitHubAPI.get_client()


def execute_command(command, **kwargs):
    """ execute command
    """
    command_split = command.split(' ')
    logger.debug(f'executing command: {command}')
    process = subprocess.run(command_split, capture_output=True, text=True, **kwargs)
    logger.debug(f'executed command: {command}')
    logger.debug(f'returncode: {process.returncode}')
    if process.stdout:
        logger.debug(f'stdout:\n{process.stdout}')
    if process.stderr:
        logger.debug(f'stderr:\n{process.stderr}')
    return process.returncode


def get_repo_data(ssh_urls):
    """ return list of repo data from ssh_urls
    """
    repos = []
    for ssh_url in ssh_urls:
        owner = ssh_url.split(':')[1].split('/')[0]
        name = ssh_url.split('/')[-1].replace('.git', '')
        item = {
            'ssh_url': ssh_url,
            'full_name': f'{owner}/{name}'
        }
        repos.append(item)
    return repos


def create_dirs():
    """ create and return required directories
    """
    scans_dir = f"{os.getenv('PWD', HOME)}/scans"
    dirs = {
        'scans': scans_dir,
        'clones': f'{scans_dir}/clones',
        'reports': f'{scans_dir}/reports'
    }
    for _, value in dirs.items():
        Path(value).mkdir(parents=True, exist_ok=True)
    return dirs


def scan_repo(process_data, *args):
    """ execute gitleaks scan on all branches of repo
    """
    repo_ssh_url = process_data['ssh_url']
    repo_full_name = process_data['full_name']
    repo_name = repo_full_name.replace('/', '-')

    logger.debug(f'scanning repo {repo_full_name}')

    client = get_client()
    branches = client.get(f'/repos/{repo_full_name}/branches', _get='all', _attributes=['name'])
    logger.debug(f'executing total of {len(branches) * 2 + 1} commands to scan repo {repo_full_name}')

    dirs = create_dirs()

    clone_dir = f"{dirs['clones']}/{repo_name}"
    shutil.rmtree(clone_dir, ignore_errors=True)
    execute_command(f'git clone {repo_ssh_url} {repo_name}', cwd=dirs['clones'])

    result = {}
    for branch in branches:
        branch_name = branch['name']
        logger.debug(f'scanning branch {branch_name} for repo {repo_full_name}')
        execute_command(f'git checkout -b {branch_name} origin/{branch_name}', cwd=clone_dir)
        safe_branch_name = branch_name.replace('/', '-')
        report = f"{dirs['reports']}/{repo_name}-{safe_branch_name}.json"
        exit_code = execute_command(f'gitleaks --path=. --branch={branch_name} --report={report} --threads=10', cwd=clone_dir)
        result[f'{repo_full_name}:{branch_name}'] = False if exit_code == 0 else report
        logger.debug(f'scanning of branch {branch_name} for repo {repo_full_name} is complete')

    logger.debug(f'scanning of repo {repo_full_name} complete')
    return result


def scan_repo_queue(process_data, *args):
    """ execute gitleaks scan on all branches of repo pulled from queue
    """
    offset = process_data['offset']
    repo_queue = process_data['repo_queue']
    dirs = create_dirs()
    client = get_client()
    zfill = len(str(repo_queue.qsize()))
    result = {}
    repo_count = 0
    while True:
        try:
            repo = repo_queue.get(timeout=10)

            repo_ssh_url = repo['ssh_url']
            repo_full_name = repo['full_name']
            repo_name = repo_full_name.replace('/', '-')

            logger.debug(f'offset {offset}| {str(repo_count).zfill(zfill)} ')
            logger.debug(f'scanning repo {repo_full_name}')
            repo_count += 1

            branches = client.get(f'/repos/{repo_full_name}/branches', _get='all', _attributes=['name'])
            logger.debug(f'executing total of {len(branches) * 2 + 1} commands to scan repo {repo_full_name}')

            clone_dir = f"{dirs['clones']}/{repo_name}"
            shutil.rmtree(clone_dir, ignore_errors=True)
            execute_command(f'git clone {repo_ssh_url} {repo_name}', cwd=dirs['clones'])

            for branch in branches:
                branch_name = branch['name']
                logger.debug(f'scanning branch {branch_name} for repo {repo_full_name}')
                execute_command(f'git checkout -b {branch_name} origin/{branch_name}', cwd=clone_dir)
                safe_branch_name = branch_name.replace('/', '-')
                report = f"{dirs['reports']}/{repo_name}-{safe_branch_name}.json"
                exit_code = execute_command(f'gitleaks --path=. --branch={branch_name} --report={report} --threads=10', cwd=clone_dir)
                result[f'{repo_full_name}:{branch_name}'] = False if exit_code == 0 else report
                logger.debug(f'scanning of branch {branch_name} for repo {repo_full_name} is complete')

            logger.debug(f'scanning of repo {repo_full_name} complete')
            # reset has the affect of resetting the progress bar
            logger.debug('RESET')

        except Empty:
            logger.debug('repo queue is empty')
            break
    logger.debug(f'scanning of repos complete - scanned {str(repo_count).zfill(zfill)} repos')
    return result


def get_results(process_data):
    """ return results from process data
    """
    results = {}
    for process in process_data:
        results.update(process['result'])
    return results


def get_process_data_queue(repos):
    """ get process data for queue processing
    """
    repo_queue = Queue()
    for repo in repos:
        repo_queue.put(repo)
    process_data = []
    for offset in range(MAX_PROCESSES):
        item = {
            'offset': str(offset).zfill(len(str(MAX_PROCESSES))),
            'repo_queue': repo_queue
        }
        process_data.append(item)
    return process_data


def execute_scans(repos, progress):
    """ execute scans for repoos using multiprocessing
    """
    if not repos:
        raise ValueError('no repos to process')

    if len(repos) <= MAX_PROCESSES:
        function = scan_repo
        process_data = repos
        max_length = max(len(item['full_name']) for item in repos)
        config = {
            'id_regex': r'^scanning repo (?P<value>.*)$',
            'id_justify': True,
            'id_width': max_length,
        }
    else:
        config = {
            'id_regex': r'^offset (?P<value>.*)$',
            'text_regex': r'scanning|executing'
        }
        function = scan_repo_queue
        process_data = get_process_data_queue(repos)

    if progress:
        config['progress_bar'] = {
            'total': r'^executing total of (?P<value>\d+) commands to scan repo .*$',
            'count_regex': r'^executed command: (?P<value>.*)$',
            'progress_message': 'scanning of all branches complete'
        }
    mp4ansi = MP4ansi(function=function, process_data=process_data, config=config)
    mp4ansi.execute(raise_if_error=True)
    return get_results(process_data)


def get_file_repos(filename):
    """ return repos read from filename
    """
    echo(f"Getting repos from file '{filename}' ...")
    if not os.access(filename, os.R_OK):
        raise ValueError(f"the default repos file '{filename}' cannot be read")
    with open(filename) as infile:
        ssh_urls = [line.strip() for line in infile.readlines()]
    repos = get_repo_data(ssh_urls)
    echo(f"A total of {len(repos)} reops were read in from '{filename}'")
    return repos


def get_user_repos(client):
    """ return repos for authenticated user
    """
    user = client.get('/user')['login']
    echo(f"Getting repos for the authenticated user '{user}' ...")
    repos = client.get('/user/repos', _get='all', _attributes=['full_name', 'ssh_url'])
    echo(f"A total of {len(repos)} reops were retrieved from authenticated user '{user}'")
    return repos


def get_org_repos(client, org):
    """ return repos for organization
    """
    echo(f"Getting repos for org: '{org}' ...")
    repos = client.get(f'/orgs/{org}/repos', _get='all', _attributes=['full_name', 'ssh_url'])
    echo(f"A total of {len(repos)} reops were retrieved from organization '{org}'")
    return repos


def get_repos(filename, user, org):
    """ get repos for filename, user or org
    """
    client = get_client()
    if user:
        repos = get_user_repos(client)
    elif org:
        repos = get_org_repos(client, org)
    else:
        repos = get_file_repos(filename)
    return repos


def match_criteria(name, include, exclude):
    """ return tuple match include and exclude on name
    """
    match_include = True
    match_exclude = False
    if include:
        match_include = re.match(include, name)
    if exclude:
        match_exclude = re.match(exclude, name)
    return match_include, match_exclude


def match_repos(repos, include, exclude):
    """ match repos using include and exclude regex
    """
    logger.debug(f'matching repos using include {include} and exclude {exclude} criteria')
    matched_repos = []
    for repo in repos:
        match_include, match_exclude = match_criteria(repo['full_name'], include, exclude)
        if match_include and not match_exclude:
            matched_repos.append(repo)
    echo(f"A total of {len(matched_repos)} repos will be processed per the inclusion/exclusion criteria")
    return matched_repos


def display_results(results):
    """ print results
    """
    if any(results.values()):
        echo('The following repos failed the gitleaks scan:')
        for scan, report in results.items():
            if report:
                home_dir = os.getenv('PWD', HOME)
                relative = report.replace(home_dir, '.')
                echo(f"{scan}:\n   {relative}")
    else:
        echo('All branches in all repos passed the gitleaks scan!')


def main():
    """ main function
    """
    args = get_parser().parse_args()
    configure_logging(args.log)

    try:
        repos = get_repos(args.filename, args.user, args.org)
        if args.include or args.exclude:
            matched_repos = match_repos(repos, args.include, args.exclude)
        else:
            matched_repos = repos
        results = execute_scans(matched_repos, args.progress)
        display_results(results)

    except Exception as exception:
        logger.error(exception)
        print(f'Error: {exception}')
        sys.exit(-1)


if __name__ == '__main__':
    main()
