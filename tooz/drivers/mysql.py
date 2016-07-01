# -*- coding: utf-8 -*-
#
# Copyright © 2014 eNovance
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
import logging

from oslo_utils import encodeutils
import pymysql

import tooz
from tooz import coordination
from tooz.drivers import _retry
from tooz import locking
from tooz import utils

LOG = logging.getLogger(__name__)


class MySQLLock(locking.Lock):
    """A MySQL based lock."""

    MYSQL_DEFAULT_PORT = 3306

    def __init__(self, name, coord):
        super(MySQLLock, self).__init__(name)
        self.coord = coord
        self._conn = MySQLDriver.get_connection(self.coord._parsed_url, self.coord._options)

    def acquire(self, blocking=True):

        @_retry.retry(stop_max_delay=blocking)
        def _lock():
            if self.acquired is True:
                if blocking:
                    raise _retry.Retry
                return False

            try:
                with self._conn as cur:
                    cur.execute("""INSERT INTO `tooz_locks`
                                (`name`, `created_at`, `created_by`)
                                values ('%s', CURRENT_TIMESTAMP, '%s');
                                """ % (str(self.name), str(self.coord._member_id)))
                    self.coord._acquired_locks.append(self)
                    return True
            except pymysql.ProgrammingError as e:
                try:
                    cur.execute("""CREATE TABLE `tooz_locks` (
				`name` varchar(255) NOT NULL
				DEFAULT '',
				`created_at` timestamp NOT NULL
				DEFAULT '0000-00-00 00:00:00',
				`updated_at` timestamp NOT NULL
				DEFAULT CURRENT_TIMESTAMP
				ON UPDATE CURRENT_TIMESTAMP,
				`created_by` varchar(255) NOT NULL
				DEFAULT '',
				PRIMARY KEY (`name`))
				ENGINE=InnoDB DEFAULT CHARSET=utf8;""")
                    raise _retry.Retry                    
                except pymysql.MySQLError as e:
                    coordination.raise_with_cause(
                        coordination.ToozError,
                        encodeutils.exception_to_unicode(e),
                        cause=e)
            except pymysql.IntegrityError as e:
                coordination.raise_with_cause(
                    coordination.LockAcquireFailed,
                    encodeutils.exception_to_unicode(e),
                    cause=e)
                
            except pymysql.MySQLError as e:
                try:
                    self._conn.connect()
                    raise _retry.Retry
                except pymysql.MySQLError as e:
                    coordination.raise_with_cause(
                        coordination.ToozError,
                        encodeutils.exception_to_unicode(e),
                        cause=e)

            if blocking:
                raise _retry.Retry
            return False

        return _lock()

    def release(self):
        if not self.acquired:
            return False
        try:
            with self._conn as cur:
                cur.execute("""DELETE FROM `tooz_locks`
                            WHERE `name` = '%s'
                            AND `created_by` = '%s';
                            """ % (str(self.name), str(self.coord._member_id)))
                self.coord._acquired_locks.remove(self)
                return True
        except pymysql.MySQLError as e:
            try:
                self._conn.connect()
                raise _retry.Retry
            except pymysql.MySQLError as e:
                coordination.raise_with_cause(
                    coordination.ToozError,
                    encodeutils.exception_to_unicode(e),
                    cause=e)

    def heartbeat(self):
        if self.aquired:
            try:
                with self.coord._conn as cur:
                    cur.execute("""UPDATE `tooz_locks`
                                WHERE `name` = %s
                                AND `created_by` = %s;
                                """ % (self.name, self.coord._member_id))
            except pymysql.MySQLError:
                try:
                    self._conn.connect()
                    raise _retry.Retry
                except pymysql.MySQLError as e:
                    coordination.raise_with_cause(
                        coordination.ToozError,
                        encodeutils.exception_to_unicode(e),
                        cause=e)
    def __del__(self):
        if self.acquired:
            self.release()
            LOG.warning("unreleased lock %s garbage collected", self.name)

    @property
    def acquired(self):
        return self in self.coord._acquired_locks

class MySQLDriver(coordination.CoordinationDriver):
    """A `MySQL`_ based driver.

    This driver users `MySQL`_ database tables to
    provide the coordination driver semantics and required API(s). It **is**
    missing some functionality but in the future these not implemented API(s)
    will be filled in.

    .. _MySQL: http://dev.mysql.com/
    """

    CHARACTERISTICS = (
        coordination.Characteristics.DISTRIBUTED_ACROSS_THREADS,
        coordination.Characteristics.DISTRIBUTED_ACROSS_PROCESSES,
        coordination.Characteristics.DISTRIBUTED_ACROSS_HOSTS,
    )
    """
    Tuple of :py:class:`~tooz.coordination.Characteristics` introspectable
    enum member(s) that can be used to interogate how this driver works.
    """

    def __init__(self, member_id, parsed_url, options):
        """Initialize the MySQL driver."""
        super(MySQLDriver, self).__init__()
        self._parsed_url = parsed_url
        self._options = utils.collapse(options)
        self._member_id = member_id
        self._acquired_locks = []

    def _start(self):
        self._conn = MySQLDriver.get_connection(self._parsed_url,
                                                self._options)

    def _stop(self):
        for _lock in self._acquired_locks:
            try:
                _lock.release()
            except:
                continue
        self._conn.close()

    def heartbeat(self):
        for _lock in self._acquired_locks:
            try:
                _lock.heartbeat()
            except:
                continue

    def get_lock(self, name):
        return MySQLLock(name, self)

    @staticmethod
    def watch_join_group(group_id, callback):
        raise tooz.NotImplemented

    @staticmethod
    def unwatch_join_group(group_id, callback):
        raise tooz.NotImplemented

    @staticmethod
    def watch_leave_group(group_id, callback):
        raise tooz.NotImplemented

    @staticmethod
    def unwatch_leave_group(group_id, callback):
        raise tooz.NotImplemented

    @staticmethod
    def watch_elected_as_leader(group_id, callback):
        raise tooz.NotImplemented

    @staticmethod
    def unwatch_elected_as_leader(group_id, callback):
        raise tooz.NotImplemented

    @staticmethod
    def get_connection(parsed_url, options):
        host = parsed_url.hostname
        port = parsed_url.port or MySQLLock.MYSQL_DEFAULT_PORT
        dbname = parsed_url.path[1:]
        username = parsed_url.username
        password = parsed_url.password
        unix_socket = options.get("unix_socket")

        try:
            if unix_socket:
                return pymysql.Connect(unix_socket=unix_socket,
                                       port=port,
                                       user=username,
                                       passwd=password,
                                       database=dbname,
                                       autocommit=True)
            else:
                return pymysql.Connect(host=host,
                                       port=port,
                                       user=username,
                                       passwd=password,
                                       database=dbname,
                                       autocommit=True)
        except (pymysql.err.OperationalError, pymysql.err.InternalError) as e:
            coordination.raise_with_cause(coordination.ToozConnectionError,
                                          encodeutils.exception_to_unicode(e),
                                          cause=e)

