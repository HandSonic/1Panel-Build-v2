"""Optional discovery-time GeoIP bytes, authenticated by the existing contract."""
import hashlib
from pathlib import Path

from automatic_frontend_lock import read_regular

CACHE = 'resolved-geoip.mmdb'
MAX_GEOIP = 64 * 1024 * 1024


def validate(raw, geoip):
    if (not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_GEOIP or
            len(raw) != geoip['bytes'] or hashlib.sha256(raw).hexdigest() != geoip['sha256']):
        raise ValueError('GeoIP cache size or digest mismatch')
    return raw


def read(directory, geoip):
    path = Path(directory) / CACHE
    if not path.exists() and not path.is_symlink():
        return None
    return validate(read_regular(path, MAX_GEOIP), geoip)


def write(directory, raw, geoip):
    raw = validate(raw, geoip)
    path = Path(directory) / CACHE
    if path.parent.is_symlink():
        raise ValueError('Linked GeoIP cache directory rejected')
    if path.exists() or path.is_symlink():
        read(directory, geoip)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as output:
        output.write(raw)
