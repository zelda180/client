import os
import math
import gzip
import tarfile

from boto.s3.connection import S3Connection
from boto.s3.key import Key
from filechunkio import FileChunkIO

from bitcalm import log
from bitcalm.api import api
from bitcalm.config import status
from bitcalm.database import get_credentials, import_db


CHUNK_SIZE = 32 * 1024 * 1024

class PREFIX_TYPE:
    FS = 'filesystem/'
    DB = 'databases/'


def next_date():
    s = next_schedule()
    return s.next_backup if s else None


def next_schedule():
    if status.schedules:
        return min([s for s in status.schedules if not s.exclude])
    return None


def get_bucket():
    conn = S3Connection(status.amazon['key_id'],
                        status.amazon['secret_key'])
    return conn.get_bucket(status.amazon['bucket'])


def get_prefix(backup_id, ptype=''):
    return '/'.join((status.amazon['username'],
                     'backup_%i' % backup_id,
                     ptype))


def get_prefixes(backup_id):
    root_prefix = get_prefix(backup_id)
    return (root_prefix,
            root_prefix + PREFIX_TYPE.FS,
            root_prefix + PREFIX_TYPE.DB)


def make_key(prefix, path):
    return '%s%s.gz' % (prefix, path.lstrip('/'))


def compress(filename, gzipped=None):
    if not gzipped:
        gzipped = '/tmp/%s.gz' % os.path.basename(filename)
    if not os.path.exists(filename):
        return ''
    with open(filename, 'rb') as f:
        with gzip.open(gzipped, 'wb') as gz:
            gz.write(f.read())
    return gzipped


def decompress(zipped, unzipped=None, delete=True):
    if not unzipped:
        unzipped = zipped[:-3]
    dirname = os.path.dirname(unzipped)
    if not os.path.exists(dirname):
        os.makedirs(dirname)
    with gzip.open(zipped, 'rb') as gz:
        with open(unzipped, 'wb') as f:
            f.write(gz.read())
    if delete:
        os.remove(zipped)
    return unzipped


def upload(key_name, filepath, delete=True):
    bucket = get_bucket()
    size = os.stat(filepath).st_size
    if size > CHUNK_SIZE:
        chunks = int(math.ceil(size / float(CHUNK_SIZE)))
        mp = bucket.initiate_multipart_upload(key_name, encrypt_key=True)
        for i in xrange(chunks):
            offset = CHUNK_SIZE * i
            psize = min(CHUNK_SIZE, size - offset)
            with FileChunkIO(filepath, mode='r',
                             offset=offset, bytes=psize) as f:
                mp.upload_part_from_file(f, part_num=i+1)
        mp.complete_upload()
    else:
        k = Key(bucket)
        k.key = key_name
        size = k.set_contents_from_filename(filepath, encrypt_key=True)
    if delete:
        os.remove(filepath)
    return size


def download(key, path):
    """ Returns:
            -1 if key wasn't found;
            0 on success;
            number of bytes in key if there is not enough free space for it.
    """
    if not isinstance(key, Key):
        b = get_bucket()
        k = b.lookup(key)
        if k:
            key = k
        else:
            return -1
    if available_space(path=os.path.dirname(path)) < key.size:
        return key.size
    key.get_contents_to_filename(path)
    return 0


def backup(key_name, filename):
    gzipped = compress(filename)
    return upload(key_name, gzipped) if gzipped else 0


def restore(backup_id):
    bucket = get_bucket()
    files = status.backupdb.files(backup_id)
    if not files:
        s, files = api.get_files_info(backup_id)
        if s == 200:
            files = files.items()
        else:
            return 'Failed to request the list of files'
    backup_prefixes = {}
    while files:
        path, b_id = files.pop()
        prefix = backup_prefixes.get(b_id)
        if not prefix:
            prefix = get_prefix(b_id, ptype=PREFIX_TYPE.FS)
            backup_prefixes[b_id] = prefix
        key = bucket.get_key(make_key(prefix, path))
        if not key:
            continue
        gzipped = '/tmp' + os.path.basename(path)
        if download(key, gzipped):
            return 'Need at least %i bytes free' % key.size
        decompress(gzipped, path)

    prefix = get_prefix(backup_id, ptype=PREFIX_TYPE.DB)
    db_keys = bucket.get_all_keys(prefix=prefix)
    db_creds = {}
    for k in db_keys:
        basename = os.path.basename(k.key)
        host, port, name = basename.split('_', 3)[:3]
        port = int(port)
        db_key = '%s:%i' % (host, port)
        if db_key not in db_creds:
            try:
                db_creds[db_key] = get_credentials(host, port)
            except ValueError:
                log.error('There are no credentials for %s:%i' % (host, port))
                continue

        gzipped = '/tmp/' + basename
        if download(k, gzipped):
            return 'Need at least %i bytes free' % k.size
        filename = decompress(gzipped)

        user, passwd = db_creds[db_key]
        if import_db(filename, user, host, passwd, port, name):
            os.remove(filename)
        else:
            log.error('Failed to import %s to %s:%i' % (name, host, port))
    return None


def available_space(path='/tmp/'):
    stats = os.statvfs(path)
    return stats.f_bavail * stats.f_frsize
