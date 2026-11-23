"""Generate a digest for the host config; never edit existing configuration."""
import getpass,hashlib
if __name__=='__main__':
    key=getpass.getpass('New proof management key (at least 16 characters): ')
    if len(key)<16 or key!=getpass.getpass('Confirm key: '):raise SystemExit('Key too short or does not match. Nothing changed.')
    print('Paste this value into [proofs] admin_key_sha256 in config.ini:')
    print(hashlib.sha256(key.encode()).hexdigest())
