# Copyright (c) Jupyter Development Team.
# Distributed under the terms of the Modified BSD License.
import hashlib
import json
import os
import os.path as osp
import re
import shlex
import shutil
import sys
import tarfile
import uuid
from glob import glob
from pathlib import Path
from subprocess import CalledProcessError
from subprocess import check_output
from tempfile import TemporaryDirectory

import click
import requests
from ghapi.all import actions_output
from ghapi.all import GhApi
from github_activity import generate_activity_md
from pep440 import is_canonical

from release_helper import __version__

HERE = osp.abspath(osp.dirname(__file__))
START_MARKER = "<!-- <START NEW CHANGELOG ENTRY> -->"
END_MARKER = "<!-- <END NEW CHANGELOG ENTRY> -->"
BUF_SIZE = 65536
TBUMP_CMD = "tbump --non-interactive --only-patch"

# Of the form:
# https://github.com/{owner}/{repo}/releases/tag/{tag}
RELEASE_HTML_PATTERN = (
    "https://github.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/tag/(?P<tag>.*)"
)

# Of the form:
# https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}
RELEASE_API_PATTERN = "https://api.github.com/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/tags/(?P<tag>.*)"

# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# Helper Functions
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""


def run(cmd, **kwargs):
    """Run a command as a subprocess and get the output as a string"""
    if not kwargs.pop("quiet", False):
        print(f"+ {cmd}")

    parts = shlex.split(cmd)
    if "/" not in parts[0]:
        parts[0] = normalize_path(shutil.which(parts[0]))

    try:
        return check_output(parts, **kwargs).decode("utf-8").strip()
    except CalledProcessError as e:
        print(e.output.decode("utf-8").strip())
        raise e


def get_branch():
    """Get the appropriat git branch"""
    if os.environ.get("GITHUB_BASE_REF"):
        # GitHub Action PR Event
        branch = os.environ["GITHUB_BASE_REF"]
    elif os.environ.get("GITHUB_REF"):
        # GitHub Action Push Event
        # e.g. refs/heads/feature-branch-1
        branch = os.environ["GITHUB_REF"].split("/")[-1]
    else:
        branch = run("git branch --show-current")
    return branch


def get_repo(remote, auth=None):
    """Get the remote repo owner and name"""
    url = run(f"git remote get-url {remote}")
    url = normalize_path(url)
    parts = url.split("/")[-2:]
    if ":" in parts[0]:
        parts[0] = parts[0].split(":")[-1]
    return "/".join(parts)


def get_version():
    """Get the current package version"""
    if osp.exists("setup.py"):
        return run("python setup.py --version")
    elif osp.exists("package.json"):
        return json.loads(Path("package.json").read_text(encoding="utf-8"))["version"]
    else:  # pragma: no cover
        raise ValueError("No version identifier could be found!")


def normalize_path(path):
    """Normalize a path to use backslashes"""
    return str(path).replace(os.sep, "/")


def format_pr_entry(target, number, auth=None):
    """Format a PR entry in the style used by our changelogs.

    Parameters
    ----------
    target : str
        The GitHub owner/repo
    number : int
        The PR number to resolve
    auth : str, optional
        The GitHub authorization token

    Returns
    -------
    str
        A formatted PR entry
    """
    owner, repo = target.split("/")
    gh = GhApi(owner=owner, repo=repo, token=auth)
    pull = gh.pulls.get(number)
    title = pull.title
    url = pull.url
    user_name = pull.user.login
    user_url = pull.user.html_url
    return f"- {title} [{number}]({url}) [@{user_name}]({user_url})"


def release_for_url(gh, url):
    """Get release response data given a release url"""
    release = None
    for release in gh.repos.list_releases():
        if release.html_url == url or release.url == url:
            release = release
    if not release:
        raise ValueError(f"No release found for url {url}")
    return release


def get_changelog_entry(branch, repo, version, *, auth=None, resolve_backports=False):
    """Get a changelog for the changes since the last tag on the given branch.

    Parameters
    ----------
    branch : str
        The target branch
    respo : str
        The GitHub owner/repo
    version : str
        The new version
    auth : str, optional
        The GitHub authorization token
    resolve_backports: bool, optional
        Whether to resolve backports to the original PR

    Returns
    -------
    str
        A formatted changelog entry with markers
    """
    since = run(f"git tag --merged {branch}")
    if not since:  # pragma: no cover
        raise ValueError(f"No tags found on branch {branch}")

    since = since.splitlines()[-1]
    print(f"Getting changes to {repo} since {since}...")

    md = generate_activity_md(repo, since=since, kind="pr", auth=auth)

    if not md:
        print("No PRs found")
        return f"## {version}\nNo merged PRs"

    md = md.splitlines()

    start = -1
    full_changelog = ""
    for (ind, line) in enumerate(md):
        if "[full changelog]" in line:
            full_changelog = line.replace("full changelog", "Full Changelog")
        elif line.strip().startswith("## Merged PRs"):
            start = ind + 1

    prs = md[start:]

    if resolve_backports:
        for (ind, line) in enumerate(prs):
            if re.search(r"\[@meeseeksmachine\]", line) is not None:
                match = re.search(r"Backport PR #(\d+)", line)
                if match:
                    prs[ind] = format_pr_entry(match.groups()[0])

    prs = "\n".join(prs).strip()

    # Move the contributor list to a heading level 3
    prs = prs.replace("## Contributors", "### Contributors")

    # Replace "*" unordered list marker with "-" since this is what
    # Prettier uses
    prs = re.sub(r"^\* ", "- ", prs)
    prs = re.sub(r"\n\* ", "\n- ", prs)

    output = f"""
## {version}

{full_changelog}

{prs}
""".strip()

    return output


def compute_sha256(path):
    """Compute the sha256 of a file"""
    sha256 = hashlib.sha256()

    with open(path, "rb") as f:
        while True:
            data = f.read(BUF_SIZE)
            if not data:
                break
            sha256.update(data)

    return sha256.hexdigest()


def create_release_commit(version):
    """Generate a release commit that has the sha256 digests for the release files"""
    cmd = f'git commit -am "Publish {version}" -m "SHA256 hashes:"'

    shas = dict()

    files = glob("dist/*")
    if not files:  # pragma: no cover
        raise ValueError("Missing distribution files")

    for path in sorted(files):
        path = normalize_path(path)
        sha256 = compute_sha256(path)
        shas[path] = sha256
        cmd += f' -m "{path}: {sha256}"'

    run(cmd)

    return shas


def bump_version(version_spec, version_cmd=""):
    """Bump the version"""
    # Look for config files to determine version command if not given
    if not version_cmd:
        for name in "bumpversion", ".bumpversion", "bump2version", ".bump2version":
            if osp.exists(name + ".cfg"):
                version_cmd = "bump2version"

        if osp.exists("tbump.toml"):
            version_cmd = version_cmd or TBUMP_CMD

        if osp.exists("pyproject.toml"):
            if "tbump" in Path("pyproject.toml").read_text(encoding="utf-8"):
                version_cmd = version_cmd or TBUMP_CMD

        if osp.exists("setup.cfg"):
            if "bumpversion" in Path("setup.cfg").read_text(encoding="utf-8"):
                version_cmd = version_cmd or "bump2version"

    if not version_cmd and osp.exists("package.json"):
        version_cmd = "npm version --git-tag-version false"

    if not version_cmd:  # pragma: no cover
        raise ValueError("Please specify a version bump command to run")

    # Bump the version
    run(f"{version_cmd} {version_spec}")


def is_prerelease(version):
    """Test whether a version is a prerelease version"""
    final_version = re.match("([0-9]+.[0-9]+.[0-9]+)", version).groups()[0]
    return final_version != version


def check_python_local(*dist_files, test_cmd=""):
    """Check a Python package locally (not as a cli)"""
    if not dist_files:
        dist_files = glob("./dist/*")
    for dist_file in dist_files:
        if Path(dist_file).suffix not in [".gz", ".whl"]:
            print(f"Skipping non-python dist file {dist_file}")
            continue
        dist_file = normalize_path(dist_file)
        run(f"twine check {dist_file}")

        if not test_cmd:
            # Get the package name from the dist file name
            name = re.match(r"(\S+)-\d", osp.basename(dist_file)).groups()[0]
            name = name.replace("-", "_")
            test_cmd = f'python -c "import {name}"'

        # Create venvs to install dist file
        # run the test command in the venv
        with TemporaryDirectory() as td:
            env_path = normalize_path(osp.abspath(td))
            if os.name == "nt":  # pragma: no cover
                bin_path = f"{env_path}/Scripts/"
            else:
                bin_path = f"{env_path}/bin"

            # Create the virtual env, upgrade pip,
            # install, and run test command
            run(f"python -m venv {env_path}")
            run(f"{bin_path}/python -m pip install -U pip")
            run(f"{bin_path}/pip install -q {dist_file}")
            run(f"{bin_path}/{test_cmd}")


def extract_npm_tarball(path):
    """Get the package json info from the tarball"""
    fid = tarfile.open(path)
    data = fid.extractfile("package/package.json").read()
    data = json.loads(data.decode("utf-8"))
    fid.close()
    return data


def build_npm_local(package):
    """Handle a local npm package (not as a cli)"""
    if not osp.exists("./package.json"):
        print("Skipping build-npm since there is no package.json file")
        return

    # Clean the dist folder of existing npm tarballs
    os.makedirs("dist", exist_ok=True)
    dest = Path("dist")
    for pkg in glob("dist/*.tgz"):
        os.remove(pkg)

    if osp.isdir(package):
        tarball = osp.join(os.getcwd(), run("npm pack"))
    else:
        tarball = package

    data = extract_npm_tarball(tarball)

    # Move the tarball into the dist folder if public
    if not data.get("private", False) == True:
        shutil.move(tarball, dest)
    elif osp.isdir(package):
        os.remove(tarball)

    if "workspaces" in data:
        packages = data["workspaces"].get("packages", [])
        for pattern in packages:
            for path in glob(pattern, recursive=True):
                path = Path(path)
                tarball = path / run("npm pack", cwd=path)
                data = extract_npm_tarball(tarball)
                if not data.get("private", False) == True:
                    shutil.move(str(tarball), str(dest))
                else:
                    os.remove(tarball)


def check_npm_local(*packages, test_cmd=None):
    if not osp.exists("./package.json"):
        print("Skipping check-npm since there is no package.json file")
        return

    if not packages:
        packages = glob("./dist/*.tgz")

    if not test_cmd:
        test_cmd = "node index.js"

    tmp_dir = Path(TemporaryDirectory().name)
    os.makedirs(tmp_dir)

    run("npm init -y", cwd=tmp_dir)
    names = []
    staging = tmp_dir / "staging"

    deps = []

    for package in packages:
        path = Path(package)
        if path.suffix != ".tgz":
            print(f"Skipping non-npm package {path.name}")
            continue

        data = extract_npm_tarball(path)
        name = data["name"]

        # Skip if it is a private package
        if data.get("private", False):  # pragma: no cover
            print(f"Skipping private package {name}")
            continue

        names.append(name)

        pkg_dir = staging / name
        if not pkg_dir.parent.exists():
            os.makedirs(pkg_dir.parent)

        tar = tarfile.open(path)
        tar.extractall(staging)
        tar.close()

        shutil.move(staging / "package", pkg_dir)

    install_str = " ".join(f"./staging/{name}" for name in names)

    run(f"npm install {install_str}", cwd=tmp_dir)

    text = "\n".join([f'require("{name}")' for name in names])
    tmp_dir.joinpath("index.js").write_text(text, encoding="utf-8")

    run(test_cmd, cwd=tmp_dir)

    shutil.rmtree(tmp_dir, ignore_errors=True)


# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
# Start CLI
# """""""""""""""""""""""""""""""""""""""""""""""""""""""""""""


class NaturalOrderGroup(click.Group):
    """Click group that lists commmands in the order added"""

    def list_commands(self, ctx):
        return self.commands.keys()


@click.group(cls=NaturalOrderGroup)
def main():
    """Release helper scripts"""
    pass


# Extracted common options
version_cmd_options = [
    click.option("--version-cmd", envvar="VERSION_CMD", help="The version command")
]

branch_options = [
    click.option("--branch", envvar="BRANCH", help="The target branch"),
    click.option(
        "--remote", envvar="REMOTE", default="upstream", help="The git remote name"
    ),
    click.option("--repo", envvar="GITHUB_REPOSITORY", help="The git repo"),
]

auth_options = [
    click.option("--auth", envvar="GITHUB_ACCESS_TOKEN", help="The GitHub auth token"),
]

dry_run_options = [
    click.option("--dry-run", is_flag=True, envvar="DRY_RUN", help="Run as a dry run")
]

changelog_path_options = [
    click.option(
        "--changelog-path",
        envvar="CHANGELOG",
        default="CHANGELOG.md",
        help="The path to changelog file",
    ),
]

changelog_options = (
    branch_options
    + auth_options
    + changelog_path_options
    + [
        click.option(
            "--resolve-backports",
            envvar="RESOLVE_BACKPORTS",
            is_flag=True,
            help="Resolve backport PRs to their originals",
        ),
    ]
)


def add_options(options):
    """Add extracted common options to a click command"""
    # https://stackoverflow.com/a/40195800
    def _add_options(func):
        for option in reversed(options):
            func = option(func)
        return func

    return _add_options


@main.command()
@add_options(version_cmd_options)
@click.option(
    "--version-spec",
    envvar="VERSION_SPEC",
    required=True,
    help="The new version specifier",
)
@add_options(branch_options)
@add_options(auth_options)
@click.option("--username", envvar="GITHUB_ACTOR", help="The git username")
@click.option("--output", envvar="GITHUB_ENV", help="Output file for env variables")
def prep_env(version_spec, version_cmd, branch, remote, repo, auth, username, output):
    """Prep git and env variables and bump version"""
    # Clear the dist directory
    shutil.rmtree("./dist", ignore_errors=True)

    # Get the branch
    branch = branch or get_branch()
    print(f"branch={branch}")

    # Get the repo
    repo = repo or get_repo(remote, auth=auth)
    print(f"repository={repo}")

    is_action = bool(os.environ.get("GITHUB_ACTIONS"))

    # Set up git config if on GitHub Actions
    if is_action:
        # Use email address for the GitHub Actions bot
        # https://github.community/t/github-actions-bot-email-address/17204/6
        run(
            'git config --global user.email "41898282+github-actions[bot]@users.noreply.github.com"'
        )
        run('git config --global user.name "GitHub Action"')

        remotes = run("git remote").splitlines()
        if remote not in remotes:
            if auth:
                url = f"http://{username}:{auth}@github.com/{repo}.git"
            else:
                url = f"http://github.com/{repo}.git"
            run(f"git remote add {remote} {url}")

    # Check out the remote branch so we can push to it
    run(f"git fetch {remote} {branch} --tags")
    branches = run("git branch").replace("* ", "").splitlines()
    if branch in branches:
        run(f"git checkout {branch}")
    else:
        run(f"git checkout -B {branch} {remote}/{branch}")

    # Bump the version
    bump_version(version_spec, version_cmd=version_cmd)

    version = get_version()

    if "setup.py" in os.listdir(".") and not is_canonical(version):  # pragma: no cover
        raise ValueError(f"Invalid version {version}")

    print(f"version={version}")
    is_prerelease_str = str(is_prerelease(version)).lower()
    print(f"is_prerelease={is_prerelease_str}")

    if output:
        print(f"Writing env variables to {output} file")
        Path(output).write_text(
            f"""
BRANCH={branch}
VERSION={version}
REPOSITORY={repo}
IS_PRERELEASE={is_prerelease_str}
""".strip(),
            encoding="utf-8",
        )


@main.command()
@add_options(changelog_options)
def build_changelog(branch, remote, repo, auth, changelog_path, resolve_backports):
    """Build changelog entry"""
    branch = branch or get_branch()

    # Get the new version
    version = get_version()

    # Get the existing changelog and run some validation
    changelog = Path(changelog_path).read_text(encoding="utf-8")

    if START_MARKER not in changelog or END_MARKER not in changelog:
        raise ValueError("Missing insert marker for changelog")

    if changelog.find(START_MARKER) != changelog.rfind(START_MARKER):
        raise ValueError("Insert marker appears more than once in changelog")

    # Get changelog entry
    repo = repo or get_repo(remote, auth=auth)
    entry = get_changelog_entry(
        f"{remote}/{branch}",
        repo,
        version,
        auth=auth,
        resolve_backports=resolve_backports,
    )

    # Insert the entry into the file
    # Test if we are augmenting an existing changelog entry (for new PRs)
    # Preserve existing PR entries since we may have formatted them
    new_entry = f"{START_MARKER}\n\n{entry}\n\n{END_MARKER}"
    prev_entry = changelog[
        changelog.index(START_MARKER) : changelog.index(END_MARKER) + len(END_MARKER)
    ]

    if f"# {version}" in prev_entry:
        lines = new_entry.splitlines()
        old_lines = prev_entry.splitlines()
        for ind, line in enumerate(lines):
            pr = re.search(r"\[#\d+\]", line)
            if not pr:
                continue
            for old_line in prev_entry.splitlines():
                if pr.group() in old_line:
                    lines[ind] = old_line
        changelog = changelog.replace(prev_entry, "\n".join(lines))
    else:
        changelog = changelog.replace(END_MARKER + "\n\n", "")
        changelog = changelog.replace(END_MARKER + "\n", "")
        changelog = changelog.replace(START_MARKER, new_entry)

    Path(changelog_path).write_text(changelog, encoding="utf-8")

    # Stage changelog
    run(f"git add {normalize_path(changelog_path)}")


@main.command()
@add_options(branch_options)
@add_options(auth_options)
@add_options(dry_run_options)
def draft_changelog(branch, remote, repo, auth, dry_run):
    """Create a changelog entry PR"""
    repo = repo or get_repo(remote, auth=auth)
    branch = branch or get_branch()
    version = get_version()

    # Check out any unstaged files from version bump
    # run("git checkout -- .")

    # Make a new branch with a uuid suffix
    pr_branch = f"changelog-{uuid.uuid1().hex}"

    if not dry_run:
        run("git stash")
        run(f"git fetch {remote} {branch}")
        run(f"git checkout -b {pr_branch} {remote}/{branch}")
        run("git stash apply")

    # Add a commit with the message
    run(f'git commit -a -m "Generate changelog for {version}"')

    # Create the pull
    owner, repo_name = repo.split("/")
    gh = GhApi(owner=owner, repo=repo_name, token=auth)
    title = f"Automated Changelog for {version} on {branch}"
    body = title

    # Check for multiple versions
    if Path("package.json").exists():
        data = json.loads(Path("package.json").read_text(encoding="utf-8"))
        if data["version"] != version:
            body += f"\nPython version: {version}"
            body += f'\nnpm version: {data["name"]}: {data["version"]}'
        if "workspaces" in data:
            body += "\nnpm workspace versions:"
            packages = data["workspaces"].get("packages", [])
            for pattern in packages:
                for path in glob(pattern, recursive=True):
                    text = Path(path / "package.json").read_text()
                    data = json.loads(text)
                    body += f'\n{data["name"]}: {data["version"]}'

    base = branch
    head = pr_branch
    maintainer_can_modify = True

    if dry_run:
        print("Skipping pull request due to dry run")
        return

    run(f"git push {remote} {pr_branch}")

    # data = dict(
    #     title=title,
    #     head=pr_branch,
    #     base=branch,
    #     body=body,
    #     maintainer_can_modify=True,
    #     draft=True,
    # )
    # gh.pulls.create(data)


@main.command()
@add_options(changelog_options)
@click.option(
    "--output", envvar="CHANGELOG_OUTPUT", help="The output file for changelog entry"
)
def check_changelog(
    branch, remote, repo, auth, changelog_path, resolve_backports, output
):
    """Check changelog entry"""
    branch = branch or get_branch()

    # Get the new version
    version = get_version()

    # Finalize changelog
    changelog = Path(changelog_path).read_text(encoding="utf-8")

    start = changelog.find(START_MARKER)
    end = changelog.find(END_MARKER)

    if start == -1 or end == -1:  # pragma: no cover
        raise ValueError("Missing new changelog entry delimiter(s)")

    if start != changelog.rfind(START_MARKER):  # pragma: no cover
        raise ValueError("Insert marker appears more than once in changelog")

    final_entry = changelog[start + len(START_MARKER) : end]

    repo = repo or get_repo(remote, auth=auth)
    raw_entry = get_changelog_entry(
        f"{remote}/{branch}",
        repo,
        version,
        auth=auth,
        resolve_backports=resolve_backports,
    )

    if f"# {version}" not in final_entry:  # pragma: no cover
        print(final_entry)
        raise ValueError(f"Did not find entry for {version}")

    final_prs = re.findall(r"\[#(\d+)\]", final_entry)
    raw_prs = re.findall(r"\[#(\d+)\]", raw_entry)

    for pr in raw_prs:
        # Allow for changelog PR to not be in changelog itself
        skip = False
        for line in raw_entry.splitlines():
            if f"[#{pr}]" in line and "changelog" in line.lower():
                skip = True
                break
        if skip:
            continue
        if not f"[#{pr}]" in final_entry:  # pragma: no cover
            raise ValueError(f"Missing PR #{pr} in changelog")
    for pr in final_prs:
        if not f"[#{pr}]" in raw_entry:  # pragma: no cover
            raise ValueError(f"PR #{pr} does not belong in changelog for {version}")

    if output:
        Path(output).write_text(final_entry, encoding="utf-8")


@main.command()
def build_python():
    """Build Python dist files"""
    # Clean the dist folder of existing npm tarballs
    os.makedirs("dist", exist_ok=True)
    dest = Path("dist")
    for pkg in glob("dist/*.gz") + glob("dist/*.whl"):
        os.remove(pkg)

    if osp.exists("./pyproject.toml"):
        run("python -m build .")
    elif osp.exists("./setup.py"):
        run("python setup.py sdist")
        run("python setup.py bdist_wheel")
    else:
        print("Skipping build-python since there are no python package files")


@main.command()
@click.argument("dist-files", nargs=-1)
@click.option(
    "--test-cmd", envvar="PY_TEST_CMD", help="The command to run in the test venvs"
)
def check_python(dist_files, test_cmd):
    """Check Python dist files"""
    check_python_local(*dist_files, test_cmd=test_cmd)


@main.command()
@click.argument("package", default=".")
def build_npm(package):
    """Build npm package"""
    build_npm_local(package)


@main.command()
@click.argument("packages", nargs=-1)
@click.option(
    "--test-cmd", envvar="NPM_TEST_CMD", help="The command to run in isolated install."
)
def check_npm(packages, test_cmd):
    """Check npm package"""
    check_npm_local(*packages, test_cmd=test_cmd)


@main.command()
def check_manifest():
    """Check the project manifest"""
    if Path("setup.py").exists() or Path("pyproject.toml").exists():
        run("check-manifest -v")
    else:
        print("Skipping build-python since there are no python package files")


@main.command()
@click.option(
    "--ignore-glob",
    envvar="IGNORE_MD",
    default=["CHANGELOG.md"],
    multiple=True,
    help="Ignore test file paths based on glob pattern",
)
@click.option(
    "--cache-file",
    envvar="CACHE_FILE",
    default="~/.cache/pytest-link-check",
    help="The cache file to use",
)
@click.option(
    "--links-expire",
    default=604800,
    envvar="LINKS_EXPIRE",
    help="Duration in seconds for links to be cached (default one week)",
)
def check_links(ignore_glob, cache_file, links_expire):
    """Check Markdown file links"""
    cache_dir = osp.expanduser(cache_file).replace(os.sep, "/")
    os.makedirs(cache_dir, exist_ok=True)
    cmd = "pytest --check-links --check-links-cache "
    cmd += f"--check-links-cache-expire-after {links_expire} "
    cmd += f"--check-links-cache-name {cache_dir}/check-release-links "
    cmd += " -k .md "

    for spec in ignore_glob:
        cmd += f"--ignore-glob {spec}"

    try:
        run(cmd)
    except Exception:
        run(cmd + " --lf")


@main.command()
@add_options(branch_options)
def tag_release(branch, remote, repo):
    """Create release commit and tag"""
    # Get the new version
    version = get_version()

    # Get the branch
    branch = branch or get_branch()

    # Create the release commit
    create_release_commit(version)

    # Create the annotated release tag
    tag_name = f"v{version}"
    run(f'git tag {tag_name} -a -m "Release {tag_name}"')


@main.command()
@add_options(branch_options)
@add_options(auth_options)
@add_options(changelog_path_options)
@add_options(version_cmd_options)
@add_options(dry_run_options)
@click.option(
    "--post-version-spec",
    envvar="POST_VERSION_SPEC",
    help="The post release version (usually dev)",
)
@click.argument("assets", nargs=-1)
def draft_release(
    branch,
    remote,
    repo,
    auth,
    changelog_path,
    version_cmd,
    dry_run,
    post_version_spec,
    assets,
):
    """Publish Draft GitHub release and handle post version bump"""
    branch = branch or get_branch()
    repo = repo or get_repo(remote, auth=auth)

    assets = assets or glob("dist/*")

    if not dry_run:
        run(f"git push {remote} HEAD:{branch} --follow-tags --tags")

    version = get_version()

    owner, repo_name = repo.split("/")
    gh = GhApi(owner=owner, repo=repo_name, token=auth)

    body = ""
    if changelog_path and Path(changelog_path).exists():
        changelog = Path(changelog_path).read_text(encoding="utf-8")

        start = changelog.find(START_MARKER)
        end = changelog.find(END_MARKER)
        if start != -1 and end != -1:
            body = changelog[start + len(START_MARKER) : end]

    # Create a draft release
    prerelease = is_prerelease(version)
    print(f"Creating release for {version}")
    release = gh.repos.create_release(
        f"v{version}",
        branch,
        f"Release v{version}",
        body,
        True,
        prerelease,
        files=assets,
    )

    # Set the GitHub action output
    print(f"\n\nSetting output release_url={release.html_url}")
    actions_output("release_url", release.html_url)

    # Bump to post version if given
    if post_version_spec:
        bump_version(post_version_spec, version_cmd)
        post_version = get_version()
        if "setup.py" in os.listdir(".") and not is_canonical(
            version
        ):  # pragma: no cover
            raise ValueError(f"\n\nInvalid post version {version}")

        print(f"Bumped version to {post_version}")
        run(f'git commit -a -m "Bump to {post_version}"')

        if not dry_run:
            run(f"git push {remote} {branch}")


@main.command()
@add_options(auth_options)
@click.argument("release-url", nargs=1)
def delete_release(auth, release_url):
    """Delete a draft GitHub release by url to the release page"""
    match = re.match(RELEASE_HTML_PATTERN, release_url)
    match = match or re.match(RELEASE_API_PATTERN, release_url)
    if not match:
        raise ValueError(f"Release url is not valid: {release_url}")

    gh = GhApi(owner=match["owner"], repo=match["repo"], token=auth)
    release = release_for_url(gh, release_url)
    for asset in release.assets:
        gh.repos.delete_release_asset(asset.id)

    # ghapi does not support deleting untagged draft releases
    headers = dict(Authorization=f"token {auth}")
    requests.delete(release.url, headers=headers)


@main.command()
@add_options(auth_options)
@click.argument("release_url", nargs=1)
def extract_release(auth, release_url):
    """Download and verify assets from a draft GitHub release"""
    match = re.match(RELEASE_HTML_PATTERN, release_url)
    match = match or re.match(RELEASE_API_PATTERN, release_url)
    if not match:  # pragma: no cover
        raise ValueError(f"Release url is not valid: {release_url}")

    gh = GhApi(owner=match["owner"], repo=match["repo"], token=auth)
    release = release_for_url(gh, release_url)

    branch = release.target_commitish
    tag = release.tag_name

    sha = None
    for tag in release.tags:
        if tag.name == release.tag_name:
            sha = tag.commit.sha

    # Run a git checkout
    # Fetch the branch
    # Get the commmit message for the branch
    commit_message = ""
    with TemporaryDirectory() as td:
        run(f"git clone {release.url} local --depth 1", cwd=td)
        checkout = osp.join(td, "local")
        if not osp.exists(release.url):
            run(f"git fetch origin {branch} --unshallow", cwd=checkout)
        commit_message = run(f"git log --format=%B -n 1 {sha}", cwd=checkout)

    # Clean the dist folder
    dist = Path("./dist")
    if dist.exists():
        shutil.rmtree(dist, ignore_errors=True)
    os.makedirs(dist)

    # Fetch, validate, and publish assets
    for asset in release.assets:
        print(f"Fetching {asset.name}...")
        url = asset.url
        headers = dict(Authorization=f"token {auth}", Accept="application/octet-stream")
        path = dist / asset.name
        with requests.get(url, headers=headers, stream=True) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        # now check the sha against the published sha
        valid = False
        sha = compute_sha256(path)

        for line in commit_message.splitlines():
            if asset.name in line:
                if sha in line:
                    valid = True
                else:
                    print("Mismatched sha!")

        if not valid:  # pragma: no cover
            raise ValueError(f"Invalid file {asset.name}")

        suffix = Path(asset.name).suffix
        if suffix in [".gz", ".whl"]:
            check_python_local(path)
        elif suffix == ".tgz":
            check_npm_local(path)
        else:
            print(f"Nothing to check for {asset.name}")


@main.command()
@add_options(auth_options)
@click.option("--npm_token", help="A token for the npm release", envvar="NPM_TOKEN")
@click.option(
    "--npm_cmd",
    help="The command to run for npm release",
    envvar="NPM_COMMAND",
    default="npm publish",
)
@click.option(
    "--twine_cmd",
    help="The twine to run for Python release",
    envvar="TWINE_COMMAND",
    default="twine upload",
)
@add_options(dry_run_options)
@click.argument("release_url", nargs=1)
def publish_release(auth, npm_token, npm_cmd, twine_cmd, dry_run, release_url):
    """Publish release asset(s) and finalize GitHub release"""
    match = re.match(RELEASE_HTML_PATTERN, release_url)
    match = match or re.match(RELEASE_API_PATTERN, release_url)
    if not match:
        raise ValueError(f"Release url is not valid: {release_url}")

    if npm_token:
        npmrc = Path(".npmrc")
        text = "//registry.npmjs.org/:_authToken={npm_token}"
        if npmrc.exists():
            text = npmrc.read_text(encoding="utf-8") + text
        npmrc.write_text(text, encoding="utf-8")

    found = False
    for path in glob("./dist/*.*"):
        name = Path(path).name
        suffix = Path(path).suffix
        path = normalize_path(path)
        if suffix in [".gz", ".whl"]:
            run(f"{twine_cmd} {path}")
            found = True
        elif suffix == ".tgz":
            run(f"{npm_cmd} {path}")
            found = True
        else:
            print(f"Nothing to upload for {name}")

    if not found:  # pragma: no cover
        raise ValueError("No assets published, refusing to finalize release")

    # Take the release out of draft
    gh = GhApi(owner=match["owner"], repo=match["repo"], token=auth)
    release = release_for_url(gh, release_url)

    release = gh.repos.update_release(
        release.id,
        release.tag_name,
        release.target_commitish,
        release.name,
        release.body,
        dry_run,
        release.prerelease,
    )

    # Set the GitHub action output
    print(f"\n\nSetting output release_url={release.html_url}")
    actions_output("release_url", release.html_url)


if __name__ == "__main__":  # pragma: no cover
    main()
