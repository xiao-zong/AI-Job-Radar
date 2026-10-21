"""Hidden iCloud app-password validation and Secret Manager storage."""
import getpass, imaplib, json, re, ssl, subprocess
PROJECT = 'your-gcp-project-id'
SECRET = 'radar-mail-icloud'
def main():
    username = input('iCloud mailbox: ').strip()
    password = getpass.getpass('Apple app-specific password (hidden): ').strip()
    if not re.fullmatch(r'[a-z]{4}(?:-[a-z]{4}){3}', password):
        print('Not an Apple app-specific password; nothing saved.'); return 1
    connection = None
    try:
        connection = imaplib.IMAP4_SSL('imap.mail.me.com', 993, ssl_context=ssl.create_default_context(), timeout=15)
        status, _ = connection.login(username, password)
        if status != 'OK': raise ValueError()
        status, _ = connection.select('"AI Job Radar"', readonly=True)
        if status != 'OK': raise ValueError()
    except Exception:
        print('iCloud login/folder check failed; nothing saved.'); return 1
    finally:
        if connection:
            try: connection.logout()
            except Exception: pass
    base = ['gcloud', 'secrets']
    check = subprocess.run(base + ['describe', SECRET, '--project', PROJECT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if check.returncode:
        create = subprocess.run(base + ['create', SECRET, '--project', PROJECT, '--replication-policy=user-managed', '--locations=australia-southeast1'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if create.returncode:
            print('Secret container could not be created; credential not saved.'); return 1
    result = subprocess.run(base + ['versions', 'add', SECRET, '--project', PROJECT, '--data-file=-', '--format=value(name)'], input=json.dumps({'username':username,'app_password':password}).encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if result.returncode:
        print('Secret Manager write failed.'); return 1
    print('ICLOUD_LOGIN_VERIFIED_AND_SECRET_SAVED', result.stdout.decode().strip())
    return 0
if __name__ == '__main__': raise SystemExit(main())
