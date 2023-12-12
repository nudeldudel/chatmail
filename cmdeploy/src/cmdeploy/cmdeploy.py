"""
Provides the `cmdeploy` entry point function,
along with command line option and subcommand parsing.
"""
import argparse
import datetime
import shutil
import subprocess
import importlib.resources
import importlib.util
import os
import sys
from pathlib import Path


from termcolor import colored
from chatmaild.config import read_config, write_initial_config


#
# cmdeploy sub commands and options
#


def init_cmd_options(parser):
    parser.add_argument(
        "chatmail_domain",
        action="store",
        help="fully qualified DNS domain name for your chatmail instance",
    )


def init_cmd(args, out):
    """Initialize chatmail config file."""
    if args.inipath.exists():
        out.red(f"Path exists, not modifying: {args.inipath}")
        raise SystemExit(1)
    write_initial_config(args.inipath, args.chatmail_domain)
    out.green(f"created config file for {args.chatmail_domain} in {args.inipath}")


def run_cmd_options(parser):
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="don't actually modify the server",
    )


def run_cmd(args, out):
    """Deploy chatmail services on the remote server."""

    env = os.environ.copy()
    env["CHATMAIL_DOMAIN"] = args.config.mail_domain
    deploy_path = "cmdeploy/src/cmdeploy/deploy.py"
    pyinf = "pyinfra --dry" if args.dry_run else "pyinfra"
    cmd = f"{pyinf} --ssh-user root {args.config.mail_domain} {deploy_path}"
    out.check_call(cmd, env=env)


def dns_cmd(args, out):
    """Generate dns zone file."""
    template = importlib.resources.files(__package__).joinpath("chatmail.zone.f")
    ssh = f"ssh root@{args.config.mail_domain}"

    def read_dkim_entries(entry):
        lines = []
        for line in entry.split("\n"):
            if line.startswith(";") or not line.strip():
                continue
            line = line.replace("\t", " ")
            lines.append(line)
        return "\n".join(lines)

    acme_account_url = out.shell_output(f"{ssh} -- acmetool account-url")
    dkim_entry = read_dkim_entries(out.shell_output(f"{ssh} -- opendkim-genzone -F"))

    out(
        f"[writing {args.config.mail_domain} zone data (using space as separator) to stdout output]",
        green=True,
    )
    print(
        template.read_text()
        .format(
            acme_account_url=acme_account_url,
            email=f"root@{args.config.mail_domain}",
            sts_id=datetime.datetime.now().strftime("%Y%m%d%H%M"),
            chatmail_domain=args.config.mail_domain,
            dkim_entry=dkim_entry,
        )
        .strip()
    )


def status_cmd(args, out):
    """Display status for online chatmail instance."""

    ssh = f"ssh root@{args.config.mail_domain}"

    out.green(f"chatmail domain: {args.config.mail_domain}")
    if args.config.privacy_mail:
        out.green("privacy settings: present")
    else:
        out.red("no privacy settings")

    s1 = "systemctl --type=service --state=running"
    for line in out.shell_output(f"{ssh} -- {s1}").split("\n"):
        if line.startswith("  "):
            print(line)


def test_cmd_options(parser):
    parser.add_argument(
        "--slow",
        dest="slow",
        action="store_true",
        help="also run slow tests",
    )


def test_cmd(args, out):
    """Run local and online tests for chatmail deployment.

    This will automatically pip-install 'deltachat' if it's not available.
    """

    x = importlib.util.find_spec("deltachat")
    if x is None:
        out.check_call(f"{sys.executable} -m pip install deltachat")

    pytest_path = shutil.which("pytest")
    pytest_args = [
        pytest_path,
        "cmdeploy/src/",
        "-n4",
        "-rs",
        "-x",
        "-vrx",
        "--durations=5",
    ]
    if args.slow:
        pytest_args.append("--slow")
    ret = out.run_ret(pytest_args)
    return ret


def fmt_cmd_options(parser):
    parser.add_argument(
        "--verbose",
        "-v",
        dest="verbose",
        action="store_true",
        help="provide information on invocations",
    )

    parser.add_argument(
        "--check",
        "-c",
        action="store_true",
        help="only check but don't fix problems",
    )


def fmt_cmd(args, out):
    """Run formattting fixes (ruff and black) on all chatmail source code."""

    sources = [str(importlib.resources.files(x)) for x in ("chatmaild", "cmdeploy")]
    black_args = [shutil.which("black")]
    ruff_args = [shutil.which("ruff")]

    if args.check:
        black_args.append("--check")
    else:
        ruff_args.append("--fix")

    if not args.verbose:
        black_args.append("-q")
        ruff_args.append("-q")

    black_args.extend(sources)
    ruff_args.extend(sources)

    out.check_call(" ".join(black_args), quiet=not args.verbose)
    out.check_call(" ".join(ruff_args), quiet=not args.verbose)
    return 0


def bench_cmd(args, out):
    """Run benchmarks against an online chatmail instance."""
    args = ["pytest", "--pyargs", "cmdeploy.tests.online.benchmark", "-vrx"]
    cmdstring = " ".join(args)
    out.green(f"[$ {cmdstring}]")
    subprocess.check_call(args)


def webdev_cmd(args, out):
    """Run local web development loop for static web pages."""
    from .www import main

    main()


#
# Parsing command line options and starting commands
#


class Out:
    """Convenience output printer providing coloring."""

    def red(self, msg, file=sys.stderr):
        print(colored(msg, "red"), file=file)

    def green(self, msg, file=sys.stderr):
        print(colored(msg, "green"), file=file)

    def __call__(self, msg, red=False, green=False, file=sys.stdout):
        color = "red" if red else ("green" if green else None)
        print(colored(msg, color), file=file)

    def shell_output(self, arg):
        self(f"[$ {arg}]", file=sys.stderr)
        return subprocess.check_output(arg, shell=True).decode()

    def check_call(self, arg, env=None, quiet=False):
        if not quiet:
            self(f"[$ {arg}]", file=sys.stderr)
        return subprocess.check_call(arg, shell=True, env=env)

    def run_ret(self, args, env=None, quiet=False):
        if not quiet:
            cmdstring = " ".join(args)
            self(f"[$ {cmdstring}]", file=sys.stderr)
        proc = subprocess.run(args, env=env)
        return proc.returncode


def add_config_option(parser):
    parser.add_argument(
        "--config",
        dest="inipath",
        action="store",
        default=Path("chatmail.ini"),
        type=Path,
        help="path to the chatmail.ini file",
    )


def add_subcommand(subparsers, func):
    name = func.__name__
    assert name.endswith("_cmd")
    name = name[:-4]
    doc = func.__doc__.strip()
    help = doc.split("\n")[0].strip(".")
    p = subparsers.add_parser(name, description=doc, help=help)
    p.set_defaults(func=func)
    add_config_option(p)
    return p


description = """
Setup your chatmail server configuration and
deploy it via SSH to your remote location.
"""


def get_parser():
    """Return an ArgumentParser for the 'cmdeploy' CLI"""

    parser = argparse.ArgumentParser(description=description.strip())
    subparsers = parser.add_subparsers(title="subcommands")

    # find all subcommands in the module namespace
    glob = globals()
    for name, func in glob.items():
        if name.endswith("_cmd"):
            subparser = add_subcommand(subparsers, func)
            addopts = glob.get(name + "_options")
            if addopts is not None:
                addopts(subparser)

    return parser


def main(args=None):
    """Provide main entry point for 'xdcget' CLI invocation."""
    parser = get_parser()
    args = parser.parse_args(args=args)
    if not hasattr(args, "func"):
        return parser.parse_args(["-h"])
    out = Out()
    kwargs = {}
    if args.func.__name__ not in ("init_cmd", "fmt_cmd"):
        if not args.inipath.exists():
            out.red(f"expecting {args.inipath} to exist, run init first?")
            raise SystemExit(1)
        try:
            args.config = read_config(args.inipath)
        except Exception as ex:
            out.red(ex)
            raise SystemExit(1)

    try:
        res = args.func(args, out, **kwargs)
        if res is None:
            res = 0
        return res

    except KeyboardInterrupt:
        out.red("KeyboardInterrupt")
        sys.exit(130)


if __name__ == "__main__":
    main()
