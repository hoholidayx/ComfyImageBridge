"""Configure SMTP from a real terminal without exposing the app password."""
import argparse
import getpass
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys
import tempfile
import warnings

from .hardware_monitor import QueueFull, configuration, queue_event, smtp_configuration
from .settings import module_root


class ConfigurationSavedError(OSError):
    """The file is published, but directory durability could not be confirmed."""


def save_configuration(root, values, password, *, replace=False):
    config, _ = configuration(root)
    reference = config.get('smtp', {})
    filename = reference.get('secret_file') if isinstance(reference, dict) else None
    if not filename:
        raise ValueError('Configure [monitor.smtp].secret_file first')
    path = Path(filename).expanduser()
    if not path.is_absolute():
        path = Path(root)/path
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('SMTP secret file must not be a symlink')
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing:
        if not stat.S_ISREG(existing.st_mode) or existing.st_uid != os.getuid():
            raise ValueError('Existing SMTP file must be a regular file owned by this user')
        if not replace:
            raise ValueError('SMTP file already exists; use --replace to update it')
    if not isinstance(password, str) or not password.strip():
        raise ValueError('App password must not be empty')
    # JSON basic strings are valid TOML strings, including quotes and backslashes.
    content = '\n'.join(f'{key} = {json.dumps(value, ensure_ascii=False)}'
                        for key, value in {**values, 'password': password}.items())+'\n'
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.smtp-', dir=path.parent)
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        validated, error = smtp_configuration(root, {'smtp': {'secret_file': temporary}})
        if not validated:
            raise ValueError(error)
        if replace:
            os.replace(temporary, path)
            temporary = None
        else:
            # Atomic no-clobber publication, including concurrent first-time setup.
            os.link(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            raise ConfigurationSavedError('SMTP file saved; directory sync failed') from None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root')
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, default=465)
    parser.add_argument('--security', choices=('ssl', 'starttls'), default='ssl')
    parser.add_argument('--username', required=True)
    parser.add_argument('--from-address')
    parser.add_argument('--to', action='append', required=True, dest='to_addresses')
    parser.add_argument('--cc', action='append', default=[], dest='cc_addresses')
    parser.add_argument('--replace', action='store_true')
    parser.add_argument('--test-email', action='store_true', help='Queue a test after saving')
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        parser.error('Run this command in an interactive terminal; the app password is hidden')
    root = module_root(args.root)
    values = {key: getattr(args, key) for key in
              ('host', 'port', 'security', 'username', 'to_addresses', 'cc_addresses')}
    values['from_address'] = args.from_address or args.username
    try:
        with warnings.catch_warnings():
            # getpass must fail closed rather than fall back to echoed stdin.
            warnings.simplefilter('error', getpass.GetPassWarning)
            password = getpass.getpass('SMTP app password (hidden): ')
        path = save_configuration(root, values, password, replace=args.replace)
        del password
    except ConfigurationSavedError:
        print('SMTP file was saved, but directory durability could not be confirmed.', file=sys.stderr)
        return 1
    except (OSError, ValueError, getpass.GetPassWarning):
        # Neither secret values nor provider exception responses belong in output.
        print('SMTP setup failed; check the private file path, permissions and settings.', file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\nSMTP setup cancelled.', file=sys.stderr)
        return 1
    print(f'SMTP configured: {path} (0600); password is not displayed.')
    if args.test_email:
        try:
            result = queue_event(root, 'test', {'message': 'SMTP configuration test'})
        except (QueueFull, sqlite3.Error, OSError, ValueError):
            print('SMTP is configured, but the test could not be queued; retry test-email.', file=sys.stderr)
            return 1
        print('Test email queued: '+str(result))
    print('The running monitor sends pending notifications automatically.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
