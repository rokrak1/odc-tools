import aiobotocore
import asyncio
from types import SimpleNamespace

from . import auto_find_region, s3_url_parse, s3_fmt_range
from ..io.async import EOS_MARKER


async def s3_fetch_object(url, s3, range=None):
    """ returns object with

     On success:
        .url = url
        .data = bytes
        .last_modified -- last modified timestamp
        .range = None | (in,out)
        .error = None

    On failure:
        .url = url
        .data = None
        .last_modified = None
        .range = None | (in, out)
        .error = str| botocore.Exception class
    """
    from botocore.exceptions import ClientError, BotoCoreError

    def result(data=None, last_modified=None, error=None):
        return SimpleNamespace(url=url, data=data, error=error, last_modified=last_modified, range=range)

    bucket, key = s3_url_parse(url)
    extra_args = {}

    if range is not None:
        try:
            extra_args['Range'] = s3_fmt_range(range)
        except Exception as e:
            return result(error='Bad range passed in: ' + str(range))

    try:
        obj = await s3.get_object(Bucket=bucket, Key=key, **extra_args)
        stream = obj.get('Body', None)
        if stream is None:
            return result(error='Missing Body in response')
        async with stream:
            data = await stream.read()
    except (ClientError, BotoCoreError) as e:
        return result(error=e)

    last_modified = obj.get('LastModified', None)
    return result(data=data, last_modified=last_modified)


def _s3_file_info(f, bucket):
    url = 's3://{}/{}'.format(bucket, f.get('Key'))
    return SimpleNamespace(url=url,
                           size=f.get('Size'),
                           last_modified=f.get('LastModified'),
                           etag=f.get('ETag'))


def _norm_predicate(pred=None, glob=None):
    from fnmatch import fnmatch

    def glob_predicate(glob, pred):
        if pred is None:
            return lambda f: fnmatch(f.url, glob)
        else:
            return lambda f: fnmatch(f.url, glob) and pred(f)

    if glob is not None:
        return glob_predicate(glob, pred)

    return pred


async def _s3_find_via_cbk(url, cbk, s3, pred=None, glob=None):
    """ List all objects under certain path

        each s3 object is represented by a SimpleNamespace with attributes:
        - url
        - size
        - last_modified
        - etag
    """
    pred = _norm_predicate(pred=pred, glob=glob)

    bucket, prefix = s3_url_parse(url)

    if not prefix.endswith('/'):
        prefix = prefix + '/'

    pp = s3.get_paginator('list_objects_v2')

    n_total, n = 0, 0

    async for o in pp.paginate(Bucket=bucket, Prefix=prefix):
        for f in o.get('Contents', []):
            n_total += 1
            f = _s3_file_info(f, bucket)
            if pred is None or pred(f):
                n += 1
                cbk(f)

    return n_total, n


async def s3_find(url, s3, pred=None, glob=None):
    """ List all objects under certain path

        each s3 object is represented by a SimpleNamespace with attributes:
        - url
        - size
        - last_modified
        - etag
    """
    _files = []

    def on_file(f):
        _files.append(f)

    await _s3_find_via_cbk(url, on_file, s3=s3, pred=pred, glob=glob)

    return _files


async def s3_dir(url, s3, pred=None, glob=None):
    """ List s3 "directory" without descending into sub directories.

        pred: predicate for file objects file_info -> True|False
        glob: glob pattern for files only

        Returns: (dirs, files)

        where
          dirs -- list of subdirectories in `s3://bucket/path/` format

          files -- list of objects with attributes: url, size, last_modified, etag
    """
    bucket, prefix = s3_url_parse(url)
    pred = _norm_predicate(pred=pred, glob=glob)

    if not prefix.endswith('/'):
        prefix = prefix + '/'

    pp = s3.get_paginator('list_objects_v2')

    _dirs = []
    _files = []

    async for o in pp.paginate(Bucket=bucket, Prefix=prefix, Delimiter='/'):
        for d in o.get('CommonPrefixes', []):
            d = d.get('Prefix')
            _dirs.append('s3://{}/{}'.format(bucket, d))
        for f in o.get('Contents', []):
            f = _s3_file_info(f, bucket)
            if pred is None or pred(f):
                _files.append(f)

    return _dirs, _files


async def s3_walker(url, nconcurrent, s3,
                    guide=None,
                    pred=None,
                    glob=None):
    """

    guide(url, depth, base) -> 'dir'|'skip'|'deep'
    """
    def default_guide(url, depth, base):
        return 'dir'

    if guide is None:
        guide = default_guide

    work_q = asyncio.Queue()
    n_active = 0

    async def step(idx):
        nonlocal n_active

        x = await work_q.get()
        if x is EOS_MARKER:
            return EOS_MARKER

        url, depth, action = x
        depth = depth + 1

        n_active += 1

        _files = []
        if action == 'dir':
            _dirs, _files = await s3_dir(url, s3=s3, pred=pred, glob=glob)

            for d in _dirs:
                action = guide(d, depth=depth, base=url)

                if action != 'skip':
                    if action not in ('dir', 'deep'):
                        raise ValueError('Expect skip|dir|deep got: %s' % action)

                    work_q.put_nowait((d, depth, action))

        elif action == 'deep':
            _files = await s3_find(url, s3=s3, pred=pred, glob=glob)
        else:
            raise RuntimeError('Expected action to be one of deep|dir but found %s' % action)

        n_active -= 1

        # Work queue was already empty and we didn't add any more to traverse
        # and no out-standing work is running
        if work_q.empty() and n_active == 0:
            # Tell all workers in the swarm to stop
            for _ in range(nconcurrent):
                work_q.put_nowait(EOS_MARKER)

        return _files

    work_q.put_nowait((url, 0, 'dir'))

    return step


class S3Fetcher(object):
    def __init__(self,
                 nconcurrent=24,
                 region_name=None,
                 addressing_style='path'):
        from ..io.async import AsyncThread
        from aiobotocore.config import AioConfig

        if region_name is None:
            region_name = auto_find_region()

        s3_cfg = AioConfig(max_pool_connections=nconcurrent,
                           s3=dict(addressing_style=addressing_style))

        self._nconcurrent = nconcurrent
        self._async = AsyncThread()
        self._s3 = None
        self._session = None
        self._closed = False

        async def setup(s3_cfg):
            session = aiobotocore.get_session()
            s3 = session.create_client('s3',
                                       region_name=region_name,
                                       config=s3_cfg)
            return (session, s3)

        session, s3 = self._async.submit(setup, s3_cfg).result()
        self._session = session
        self._s3 = s3

    def close(self):
        async def _close(s3):
            await s3.close()

        if not self._closed:
            self._async.submit(_close, self._s3).result()
            self._async.terminate()
            self._closed = True

    def __del__(self):
        self.close()

    def list_dir(self, url):
        """ Returns a future object
        """
        async def action(url, s3):
            return await s3_dir(url, s3=s3)
        return self._async.submit(action, url, self._s3)

    def find(self, url, pred=None, glob=None):
        """ List all objects under certain path

        Returns a future object that resolves to a list of s3 object metadata

        each s3 object is represented by a SimpleNamespace with attributes:
        - url
        - size
        - last_modified
        - etag
        """
        if glob is None and isinstance(pred, str):
            pred, glob = None, pred

        async def action(url, s3):
            return await s3_find(url, s3=s3, pred=pred, glob=glob)

        return self._async.submit(action, url, self._s3)

    def fetch(self, url, range=None):
        """ Returns a future object
        """
        return self._async.submit(s3_fetch_object, url, s3=self._s3, range=range)

    def __call__(self, urls):
        """Fetch a bunch of s3 urls concurrently.

        urls -- sequence of  <url | (url, range)> , where range is (in:int,out:int)|None

        On output is a sequence of result objects, note that order is not
        preserved, but one should get one result for every input.

        Successful results object will contain:
          .url = url
          .data = bytes
          .last_modified -- last modified timestamp
          .range = None | (in,out)
          .error = None

        Failed result looks like this:
          .url = url
          .data = None
          .last_modified = None
          .range = None | (in, out)
          .error = str| botocore.Exception class

        """
        from ..ppr import future_results

        def generate_requests(urls, s3):
            for url in urls:
                if isinstance(url, tuple):
                    url, range = url
                else:
                    range = None

                yield self._async.submit(s3_fetch_object, url, s3=s3, range=range)

        for rr, ee in future_results(generate_requests(urls, self._s3), self._nconcurrent*2):
            if ee is not None:
                assert(not "s3_fetch_object should not raise exceptions, but did")
            else:
                yield rr
