#!/usr/bin/python
# -*- coding: utf-8 -*-
#
#  __init__.py
#
#
#  Created by Vincent Anh Tran on 21/03/2018
#  Copyright (c) Vincent Anh Tran - maintain this project since 0.1.1
#
from __future__ import absolute_import

import copy
import logging
import weakref
from threading import Lock

from amqp.exceptions import ConnectionError
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from nameko.standalone.rpc import ClusterRpcProxy
from six.moves import queue as queue_six
from six.moves import xrange as xrange_six

_logger = logging.getLogger(__name__)


class ClusterRpcProxyPool(object):
    """ Connection pool for Nameko RPC cluster.

    Pool size can be customized by passing `pool_size` kwarg to constructor.
    Default size is 4.

    *Usage*

        pool = ClusterRpcProxyPool(config)
        pool.start()

        # ...

        with pool.next() as rpc:
            rpc.mailer.send_mail(foo='bar')

        # ...

        pool.stop()

    This class is thread-safe and designed to work with GEvent.
    """

    class RpcContext(object):
        def __init__(self, pool, config):
            self._pool = weakref.proxy(pool)
            self._proxy = ClusterRpcProxy(config, context_data=copy.deepcopy(pool.context_data), timeout=pool.timeout)
            self._rpc = self._proxy.start()
            self._enable_rpc_call = False

        def __del__(self):

            try:
                self._proxy.stop()
            except AttributeError:
                pass
            self._proxy = None
            self._rpc = None

        def __getattr__(self, item):
            """ This will return the service proxy instance

            :param item: name of the service
            :return: Service Proxy
            """
            if not self._enable_rpc_call:
                raise AttributeError(item)
            return getattr(self._rpc, item)

        def __enter__(self):
            self._enable_rpc_call = True
            return weakref.proxy(self)

        def __exit__(self, exc_type, exc_value, traceback, **kwargs):
            self._enable_rpc_call = False
            try:
                if exc_type == RuntimeError and (
                        str(exc_value) == "This consumer has been stopped, and can no longer be used"
                        or str(exc_value) == "This consumer has been disconnected, and can no longer be used"):
                    self._pool._clear()
                    self._pool._reload()  # reload all worker
                    self.__del__()
                elif exc_type == ConnectionError:  # maybe check for RpcTimeout, as well
                    # self.pool._clear()
                    self._pool._reload(1)  # reload atmost 1 worker
                    self.__del__()
                else:
                    if self._rpc._worker_ctx.data is not None:
                        if self._pool.context_data is None:
                            # clear all key since there is no.pool context_data
                            for key in self._rpc._worker_ctx.data.keys():
                                del self._rpc._worker_ctx.data[key]
                        elif len(self._rpc._worker_ctx.data) != len(self._pool.context_data) \
                                or self._rpc._worker_ctx.data != self._pool.context_data:
                            # ensure that worker_ctx.data is revert back to original
                            # pool.context_data when exit of block
                            for key in self._rpc._worker_ctx.data.keys():
                                if key not in self._pool.context_data:
                                    del self._rpc._worker_ctx.data[key]
                                else:
                                    self._rpc._worker_ctx.data[key] = self._pool.context_data[key]
                    self._pool._put_back(self)
            except ReferenceError:  # pragma: no cover
                # We're detached from the parent, so this context
                # is going to silently die.
                self.__del__()

    def __init__(self, config, pool_size=None, context_data=None, timeout=0):
        if pool_size is None:  # keep this for compatiblity
            pool_size = getattr(settings, 'NAMEKO_POOL_SIZE', 4)
        if context_data is None:  # keep this for compatiblity
            context_data = getattr(settings, 'NAMEKO_CONTEXT_DATA', None)
        if timeout is None or timeout <= 0:  # keep this for compatiblity
            timeout = getattr(settings, 'NAMEKO_TIMEOUT', None)
        self.config = copy.deepcopy(config)
        self.pool_size = pool_size
        self.context_data = copy.deepcopy(context_data)
        self.timeout = timeout
        self.state = 'NOT_STARTED'

    def start(self):
        """ Populate pool with connections.
        """
        self.queue = queue_six.Queue()
        for i in xrange_six(self.pool_size):
            ctx = ClusterRpcProxyPool.RpcContext(self, self.config)
            self.queue.put(ctx)
        self.state = 'STARTED'

    @property
    def is_started(self):
        return self.state != 'NOT_STARTED'

    def _clear(self):
        count = 0
        while self.queue.empty() is False:
            self.next()
            count += 1
        _logger.debug("Clear %d worker", count)

    def _reload(self, num_of_worker=0):
        """ Reload into pool's queue with number of new worker

        :param int num_of_worker:
        :return: None
        """
        if num_of_worker <= 0:
            num_of_worker = self.pool_size
        count = 0
        for i in xrange_six(num_of_worker):
            if self.queue.full() is False:
                ctx = ClusterRpcProxyPool.RpcContext(self, self.config)
                self.queue.put_nowait(ctx)
                count += 1
        _logger.debug("Reload %d worker", count)

    def next(self, block=True, timeout=None):
        """ Fetch next connection.

        This method is thread-safe.
        :rtype: ClusterRpcProxyPool.RpcContext
        """
        return self.queue.get(block=block, timeout=timeout)

    def _put_back(self, ctx):
        self.queue.put(ctx)

    def stop(self):
        """ Stop queue and remove all connections from pool.
        """
        while True:
            try:
                ctx = self.queue.get_nowait()
                ctx.__del__()
            except queue_six.Empty:
                break
        self.queue.queue.clear()
        self.queue = None


nameko_global_pools = None
create_pool_lock = Lock()


def mergedicts(dict1, dict2):
    for k in set(dict1.keys()).union(dict2.keys()):
        if k in dict1 and k in dict2:
            if isinstance(dict1[k], dict) and isinstance(dict2[k], dict):
                yield (k, dict(mergedicts(dict1[k], dict2[k])))
            else:
                # If one of the values is not a dict, you can't continue merging it.
                # Value from second dict overrides one in first and we move on.
                yield (k, dict2[k])
                # Alternatively, replace this with exception raiser to alert you of value conflicts
        elif k in dict1:
            yield (k, dict1[k])
        else:
            yield (k, dict2[k])


def get_pool(pool_name=None):
    """
    Use this method to acquire a connection pool from nameko_global_pools.

    Example usage:

        from coreservices.core.rpc import get_pool
        # ...
        with get_pool().next() as rpc:
            rpc.mailer.send_mail(foo='bar')
    :rtype: ClusterRpcProxyPool
    """

    global nameko_global_pools

    if not nameko_global_pools:
        NAMEKO_CONFIG = getattr(settings, 'NAMEKO_CONFIG', {})
        if not NAMEKO_CONFIG:
            raise ImproperlyConfigured('NAMEKO_CONFIG must be specified')
        NAMEKO_MULTI_POOL = [name for name in NAMEKO_CONFIG.keys() if name.islower()]
        # Lazy instantiation, acquire lock first to prevent dupication init
        with create_pool_lock:
            if not nameko_global_pools:  # double check inside lock is importance
                if NAMEKO_MULTI_POOL:
                    nameko_global_pools = dict()
                    if 'default' not in NAMEKO_CONFIG or 'AMQP_URL' not in NAMEKO_CONFIG['default']:
                        raise ImproperlyConfigured(
                            'NAMEKO_CONFIG must be specified and should '
                            'include at least "default" config with "AMQP_URL"')
                    default_config = NAMEKO_CONFIG['default']
                    # default_context_data = NAMEKO_CONFIG['default']['POOL'].get('CONTEXT_DATA', dict())
                    # multi_context_data = getattr(settings, 'NAMEKO_MULTI_CONTEXT_DATA', dict())
                    for name, _config in NAMEKO_CONFIG.items():
                        # each nameko_global_pools will have different config with default config as default
                        if name != 'default':
                            # overide default config with nameko_global_pools config by merging 2 dict
                            pool_config = dict(mergedicts(default_config.copy(), _config))
                        else:
                            # default nameko_global_pools
                            pool_config = default_config.copy()
                        # extract nameko_global_pools config from RpcCluster config
                        pool_size = pool_config.pop('POOL_SIZE', None)
                        pool_context_data = pool_config.pop('POOL_CONTEXT_DATA', None)
                        pool_timeout = pool_config.pop('POOL_TIMEOUT', 0)
                        # init nameko_global_pools
                        _pool = ClusterRpcProxyPool(pool_config, pool_size=pool_size, context_data=pool_context_data,
                                                    timeout=pool_timeout)
                        _pool.start()
                        # assign nameko_global_pools to corresponding name
                        nameko_global_pools[name] = _pool
                else:
                    # single nameko_global_pools with old style configuration

                    nameko_global_pools = ClusterRpcProxyPool(settings.NAMEKO_CONFIG)
                    nameko_global_pools.start()  # start immediately
        # Finish instantiation, release lock

    if pool_name is not None:
        if isinstance(nameko_global_pools, dict) is False or pool_name not in nameko_global_pools:
            raise ImproperlyConfigured(
                'NAMEKO_CONFIG must include this nameko_global_pools name "%s" config' % pool_name)
        else:
            _pool = nameko_global_pools[pool_name]
    else:
        if isinstance(nameko_global_pools, dict):
            if len(nameko_global_pools) == 0:  # pragma: nocover
                # this code is unreachable, it's not passilbe to have a dict without a key in it.
                raise ImproperlyConfigured('NAMEKO_CONFIG must include at least 1 "default" config')
            _pool = nameko_global_pools.get('default', next(iter(nameko_global_pools.values())))
        else:
            _pool = nameko_global_pools

    return _pool


def destroy_pool():
    global nameko_global_pools
    if isinstance(nameko_global_pools, dict):
        for pool in nameko_global_pools.values():
            pool.stop()
    elif nameko_global_pools is not None:
        nameko_global_pools.stop()
    nameko_global_pools = None
