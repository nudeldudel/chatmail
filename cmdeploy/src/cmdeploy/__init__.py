"""
Chat Mail pyinfra deploy.
"""
import sys
import importlib.resources
import subprocess
import shutil
import io
from pathlib import Path

from pyinfra import host
from pyinfra.operations import apt, files, server, systemd, pip
from pyinfra.facts.files import File
from pyinfra.facts.systemd import SystemdEnabled
from .acmetool import deploy_acmetool

from chatmaild.config import read_config, Config


def _build_chatmaild(dist_dir) -> None:
    dist_dir = Path(dist_dir).resolve()
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir()
    subprocess.check_output(
        [sys.executable, "-m", "build", "-n"]
        + ["--sdist", "chatmaild", "--outdir", str(dist_dir)]
    )
    entries = list(dist_dir.iterdir())
    assert len(entries) == 1
    return entries[0]


def remove_legacy_artifacts():
    # disable legacy doveauth-dictproxy.service
    if host.get_fact(SystemdEnabled).get("doveauth-dictproxy.service"):
        systemd.service(
            name="Disable legacy doveauth-dictproxy.service",
            service="doveauth-dictproxy.service",
            running=False,
            enabled=False,
        )


def _install_remote_venv_with_chatmaild(config) -> None:
    remove_legacy_artifacts()
    dist_file = _build_chatmaild(dist_dir=Path("chatmaild/dist"))
    remote_base_dir = "/usr/local/lib/chatmaild"
    remote_dist_file = f"{remote_base_dir}/dist/{dist_file.name}"
    remote_venv_dir = f"{remote_base_dir}/venv"
    remote_chatmail_inipath = f"{remote_base_dir}/chatmail.ini"
    root_owned = dict(user="root", group="root", mode="644")

    apt.packages(
        name="apt install python3-virtualenv",
        packages=["python3-virtualenv"],
    )

    files.put(
        name="Upload chatmaild source package",
        src=dist_file.open("rb"),
        dest=remote_dist_file,
        create_remote_dir=True,
        **root_owned,
    )

    files.put(
        name=f"Upload {remote_chatmail_inipath}",
        src=config._getbytefile(),
        dest=remote_chatmail_inipath,
        **root_owned,
    )

    pip.virtualenv(
        name=f"chatmaild virtualenv {remote_venv_dir}",
        path=remote_venv_dir,
        always_copy=True,
    )

    server.shell(
        name=f"forced pip-install {dist_file.name}",
        commands=[
            f"{remote_venv_dir}/bin/pip install --force-reinstall {remote_dist_file}"
        ],
    )

    files.template(
        src=importlib.resources.files(__package__).joinpath("metrics.cron.j2"),
        dest="/etc/cron.d/chatmail-metrics",
        user="root",
        group="root",
        mode="644",
        config={
            "mail_domain": config.mail_domain,
            "execpath": f"{remote_venv_dir}/bin/chatmail-metrics",
        },
    )

    # install systemd units
    for fn in (
        "doveauth",
        "filtermail",
        "echobot",
    ):
        params = dict(
            execpath=f"{remote_venv_dir}/bin/{fn}",
            config_path=remote_chatmail_inipath,
            remote_venv_dir=remote_venv_dir,
        )
        source_path = importlib.resources.files("chatmaild").joinpath(f"{fn}.service.f")
        content = source_path.read_text().format(**params).encode()

        files.put(
            name=f"Upload {fn}.service",
            src=io.BytesIO(content),
            dest=f"/etc/systemd/system/{fn}.service",
            **root_owned,
        )
        systemd.service(
            name=f"Setup {fn} service",
            service=f"{fn}.service",
            running=True,
            enabled=True,
            restarted=True,
            daemon_reload=True,
        )


def _configure_opendkim(domain: str, dkim_selector: str = "dkim") -> bool:
    """Configures OpenDKIM"""
    need_restart = False

    main_config = files.template(
        src=importlib.resources.files(__package__).joinpath("opendkim/opendkim.conf"),
        dest="/etc/opendkim.conf",
        user="root",
        group="root",
        mode="644",
        config={"domain_name": domain, "opendkim_selector": dkim_selector},
    )
    need_restart |= main_config.changed

    files.directory(
        name="Add opendkim directory to /etc",
        path="/etc/opendkim",
        user="opendkim",
        group="opendkim",
        mode="750",
        present=True,
    )

    keytable = files.template(
        src=importlib.resources.files(__package__).joinpath("opendkim/KeyTable"),
        dest="/etc/dkimkeys/KeyTable",
        user="opendkim",
        group="opendkim",
        mode="644",
        config={"domain_name": domain, "opendkim_selector": dkim_selector},
    )
    need_restart |= keytable.changed

    signing_table = files.template(
        src=importlib.resources.files(__package__).joinpath("opendkim/SigningTable"),
        dest="/etc/dkimkeys/SigningTable",
        user="opendkim",
        group="opendkim",
        mode="644",
        config={"domain_name": domain, "opendkim_selector": dkim_selector},
    )
    need_restart |= signing_table.changed

    files.directory(
        name="Add opendkim socket directory to /var/spool/postfix",
        path="/var/spool/postfix/opendkim",
        user="opendkim",
        group="opendkim",
        mode="750",
        present=True,
    )

    if not host.get_fact(File, f"/etc/dkimkeys/{dkim_selector}.private"):
        server.shell(
            name="Generate OpenDKIM domain keys",
            commands=[
                f"opendkim-genkey -D /etc/dkimkeys -d {domain} -s {dkim_selector}"
            ],
            _sudo=True,
            _sudo_user="opendkim",
        )

    return need_restart


def _install_mta_sts_daemon() -> bool:
    need_restart = False

    config = files.put(
        name="upload postfix-mta-sts-resolver config",
        src=importlib.resources.files(__package__).joinpath(
            "postfix/mta-sts-daemon.yml"
        ),
        dest="/etc/mta-sts-daemon.yml",
        user="root",
        group="root",
        mode="644",
    )
    need_restart |= config.changed

    server.shell(
        name="install postfix-mta-sts-resolver with pip",
        commands=[
            "python3 -m virtualenv /usr/local/lib/postfix-mta-sts-resolver",
            "/usr/local/lib/postfix-mta-sts-resolver/bin/pip install postfix-mta-sts-resolver",
        ],
    )

    systemd_unit = files.put(
        name="upload mta-sts-daemon systemd unit",
        src=importlib.resources.files(__package__).joinpath(
            "postfix/mta-sts-daemon.service"
        ),
        dest="/etc/systemd/system/mta-sts-daemon.service",
        user="root",
        group="root",
        mode="644",
    )
    need_restart |= systemd_unit.changed

    return need_restart


def _configure_postfix(config: Config, debug: bool = False) -> bool:
    """Configures Postfix SMTP server."""
    need_restart = False

    main_config = files.template(
        src=importlib.resources.files(__package__).joinpath("postfix/main.cf.j2"),
        dest="/etc/postfix/main.cf",
        user="root",
        group="root",
        mode="644",
        config=config,
    )
    need_restart |= main_config.changed

    master_config = files.template(
        src=importlib.resources.files(__package__).joinpath("postfix/master.cf.j2"),
        dest="/etc/postfix/master.cf",
        user="root",
        group="root",
        mode="644",
        debug=debug,
        config=config,
    )
    need_restart |= master_config.changed

    return need_restart


def _configure_dovecot(config: Config, debug: bool = False) -> bool:
    """Configures Dovecot IMAP server."""
    need_restart = False

    main_config = files.template(
        src=importlib.resources.files(__package__).joinpath("dovecot/dovecot.conf.j2"),
        dest="/etc/dovecot/dovecot.conf",
        user="root",
        group="root",
        mode="644",
        config=config,
        debug=debug,
    )
    need_restart |= main_config.changed
    auth_config = files.put(
        src=importlib.resources.files(__package__).joinpath("dovecot/auth.conf"),
        dest="/etc/dovecot/auth.conf",
        user="root",
        group="root",
        mode="644",
    )
    need_restart |= auth_config.changed

    files.template(
        src=importlib.resources.files(__package__).joinpath("dovecot/expunge.cron.j2"),
        dest="/etc/cron.d/expunge",
        user="root",
        group="root",
        mode="644",
        config=config,
    )

    # as per https://doc.dovecot.org/configuration_manual/os/
    # it is recommended to set the following inotify limits
    for name in ("max_user_instances", "max_user_watches"):
        key = f"fs.inotify.{name}"
        server.sysctl(
            name=f"Change {key}",
            key=key,
            value=65535,
            persist=True,
        )

    return need_restart


def _configure_nginx(domain: str, debug: bool = False) -> bool:
    """Configures nginx HTTP server."""
    need_restart = False

    main_config = files.template(
        src=importlib.resources.files(__package__).joinpath("nginx/nginx.conf.j2"),
        dest="/etc/nginx/nginx.conf",
        user="root",
        group="root",
        mode="644",
        config={"domain_name": domain},
    )
    need_restart |= main_config.changed

    autoconfig = files.template(
        src=importlib.resources.files(__package__).joinpath("nginx/autoconfig.xml.j2"),
        dest="/var/www/html/.well-known/autoconfig/mail/config-v1.1.xml",
        user="root",
        group="root",
        mode="644",
        config={"domain_name": domain},
    )
    need_restart |= autoconfig.changed

    mta_sts_config = files.template(
        src=importlib.resources.files(__package__).joinpath("nginx/mta-sts.txt.j2"),
        dest="/var/www/html/.well-known/mta-sts.txt",
        user="root",
        group="root",
        mode="644",
        config={"domain_name": domain},
    )
    need_restart |= mta_sts_config.changed

    # install CGI newemail script
    #
    cgi_dir = "/usr/lib/cgi-bin"
    files.directory(
        name=f"Ensure {cgi_dir} exists",
        path=cgi_dir,
        user="root",
        group="root",
    )

    files.put(
        name="Upload cgi newemail.py script",
        src=importlib.resources.files("chatmaild").joinpath("newemail.py").open("rb"),
        dest=f"{cgi_dir}/newemail.py",
        user="root",
        group="root",
        mode="755",
    )

    return need_restart


def remove_opendkim() -> bool:
    """Remove OpenDKIM, deprecated"""
    files.file(
        name="Remove legacy opendkim.conf",
        path="/etc/opendkim.conf",
        present=False,
    )

    files.directory(
        name="Remove legacy opendkim socket directory from /var/spool/postfix",
        path="/var/spool/postfix/opendkim",
        present=False,
    )

    apt.packages(
        name="Remove openDKIM",
        packages="opendkim",
        present=False
    )
    return False


def _configure_rspamd(dkim_selector: str, mail_domain: str) -> bool:
    """Configures rspamd for Rate Limiting."""
    need_restart = False

    phishing_conf = files.put(
        name="disable phishing rspamd plugin",
        src=importlib.resources.files(__package__).joinpath("rspamd/phishing.conf"),
        dest="/etc/rspamd/local.d/phishing.conf",
        user="root",
        group="root",
        mode="644",
    )
    need_restart |= phishing_conf.changed

    rbl = files.put(
        name="disable rbl rspamd plugin",
        src=importlib.resources.files(__package__).joinpath("rspamd/rbl.conf"),
        dest="/etc/rspamd/override.d/rbl.conf",
        user="root",
        group="root",
        mode="644",
    )
    need_restart |= rbl.changed

    options_inc = files.put(
        name="disable fuzzy checks",
        src=importlib.resources.files(__package__).joinpath("rspamd/options.inc"),
        dest="/etc/rspamd/local.d/options.inc",
        user="root",
        group="root",
        mode="644",
    )
    need_restart |= options_inc.changed

    hfilter = files.put(
        name="disable hfilter rspamd plugin",
        src=importlib.resources.files(__package__).joinpath("rspamd/hfilter.conf"),
        dest="/etc/rspamd/local.d/hfilter.conf",
        user="root",
        group="root",
        mode="644",
    )
    need_restart |= hfilter.changed

    ratelimit_conf = files.put(
        name="enable rate limiting",
        src=importlib.resources.files(__package__).joinpath("rspamd/ratelimit.conf"),
        dest="/etc/rspamd/local.d/ratelimit.conf",
        user="root",
        group="root",
        mode="644",
    )
    need_restart |= ratelimit_conf.changed

    dkim_directory = "/var/lib/rspamd/dkim/"
    dkim_key_path = f"{dkim_directory}{mail_domain}.{dkim_selector}.key"

    dkim_config = files.template(
        src=importlib.resources.files(__package__).joinpath("rspamd/dkim_signing.conf.j2"),
        dest="/etc/rspamd/local.d/dkim_signing.conf",
        user="root",
        group="root",
        mode="644",
        config={
            "dkim_selector": str(dkim_selector),
            "mail_domain": mail_domain,
            "dkim_key_path": dkim_key_path,
        },
    )
    need_restart |= dkim_config.changed

    files.directory(
        name="ensure DKIM key directory exists",
        path=dkim_directory,
        present=True,
        user="_rspamd",
        group="_rspamd",
    )

    if not host.get_fact(File, dkim_key_path):
        server.shell(
            name="Generate DKIM domain keys with rspamd",
            commands=[
                f"rspamadm dkim_keygen -s {dkim_selector} -d {mail_domain} -k {dkim_key_path}"
            ],
            _sudo=True,
            _sudo_user="_rspamd",
        )

    return need_restart


def _configure_redis() -> bool:
    """Configures redis as a key-value storage for rspamd."""
    need_restart = False

    redis_config = files.put(
        src=importlib.resources.files(__package__).joinpath("rspamd/redis.conf"),
        dest="/etc/redis/redis.conf",
        user="redis",
        group="redis",
        mode="640",
    )
    need_restart |= redis_config.changed

    return need_restart


def check_config(config):
    mail_domain = config.mail_domain
    if mail_domain != "testrun.org" and not mail_domain.endswith(".testrun.org"):
        blocked_words = "merlinux schmieder testrun.org".split()
        for value in config.__dict__.values():
            if any(x in str(value) for x in blocked_words):
                raise ValueError(
                    f"please set your own privacy contacts/addresses in {config._inipath}"
                )
    return config


def deploy_chatmail(config_path: Path) -> None:
    """Deploy a chat-mail instance.

    :param config_path: path to chatmail.ini
    """
    config = read_config(config_path)
    check_config(config)
    mail_domain = config.mail_domain

    from .www import build_webpages

    apt.update(name="apt update", cache_time=24 * 3600)
    server.group(name="Create vmail group", group="vmail", system=True)
    server.user(name="Create vmail user", user="vmail", group="vmail", system=True)

    server.group(name="Create opendkim group", group="opendkim", system=True)
    server.user(
        name="Add postfix user to opendkim group for socket access",
        user="postfix",
        groups=["opendkim"],
        system=True,
    )

    # Run local DNS resolver `unbound`.
    # `resolvconf` takes care of setting up /etc/resolv.conf
    # to use 127.0.0.1 as the resolver.
    apt.packages(
        name="Install unbound",
        packages=["unbound", "unbound-anchor", "dnsutils"],
    )
    server.shell(
        name="Generate root keys for validating DNSSEC",
        commands=["unbound-anchor -a /var/lib/unbound/root.key || true"],
    )
    systemd.service(
        name="Start and enable unbound",
        service="unbound.service",
        running=True,
        enabled=True,
    )

    # Deploy acmetool to have TLS certificates.
    deploy_acmetool(nginx_hook=True, domains=[mail_domain, f"mta-sts.{mail_domain}"])

    apt.packages(
        name="Install Postfix",
        packages="postfix",
    )

    apt.packages(
        name="Install Dovecot",
        packages=["dovecot-imapd", "dovecot-lmtpd"],
    )

    apt.packages(
        name="Install OpenDKIM",
        packages=[
            "opendkim",
            "opendkim-tools",
        ],
    )

    apt.packages(
        name="Install nginx",
        packages=["nginx"],
    )

    apt.packages(
        name="Install fcgiwrap",
        packages=["fcgiwrap"],
    )

    www_path = importlib.resources.files(__package__).joinpath("../../../www").resolve()

    build_dir = www_path.joinpath("build")
    src_dir = www_path.joinpath("src")
    build_webpages(src_dir, build_dir, config)
    files.rsync(f"{build_dir}/", "/var/www/html", flags=["-avz"])

    _install_remote_venv_with_chatmaild(config)
    debug = False
    dovecot_need_restart = _configure_dovecot(config, debug=debug)
    postfix_need_restart = _configure_postfix(config, debug=debug)
    mta_sts_need_restart = _install_mta_sts_daemon()
    nginx_need_restart = _configure_nginx(mail_domain)

    remove_opendkim()
    rspamd_need_restart = _configure_rspamd("dkim", mail_domain)
    redis_need_restart = _configure_redis()

    systemd.service(
        name="Start and enable redis-server",
        service="redis-server.service",
        running=True,
        enabled=True,
        restarted=redis_need_restart,
    )

    systemd.service(
        name="Start and enable rspamd",
        service="rspamd.service",
        running=True,
        enabled=True,
        restarted=rspamd_need_restart,
    )


    systemd.service(
        name="Start and enable MTA-STS daemon",
        service="mta-sts-daemon.service",
        daemon_reload=True,
        running=True,
        enabled=True,
        restarted=mta_sts_need_restart,
    )

    systemd.service(
        name="Start and enable Postfix",
        service="postfix.service",
        running=True,
        enabled=True,
        restarted=postfix_need_restart,
    )

    systemd.service(
        name="Start and enable Dovecot",
        service="dovecot.service",
        running=True,
        enabled=True,
        restarted=dovecot_need_restart,
    )

    systemd.service(
        name="Start and enable nginx",
        service="nginx.service",
        running=True,
        enabled=True,
        restarted=nginx_need_restart,
    )

    # This file is used by auth proxy.
    # https://wiki.debian.org/EtcMailName
    server.shell(
        name="Setup /etc/mailname",
        commands=[f"echo {mail_domain} >/etc/mailname; chmod 644 /etc/mailname"],
    )

    journald_conf = files.put(
        name="Configure journald",
        src=importlib.resources.files(__package__).joinpath("journald.conf"),
        dest="/etc/systemd/journald.conf",
        user="root",
        group="root",
        mode="644",
    )
    systemd.service(
        name="Start and enable journald",
        service="systemd-journald.service",
        running=True,
        enabled=True,
        restarted=journald_conf,
    )
