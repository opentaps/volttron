# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:
#
# Copyright (c) 2016, Battelle Memorial Institute
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA,
# OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are
# those of the authors and should not be interpreted as representing
# official policies, either expressed or implied, of the FreeBSD Project.
#

# This material was prepared as an account of work sponsored by an
# agency of the United States Government.  Neither the United States
# Government nor the United States Department of Energy, nor Battelle,
# nor any of their employees, nor any jurisdiction or organization
# that has cooperated in the development of these materials, makes
# any warranty, express or implied, or assumes any legal liability
# or responsibility for the accuracy, completeness, or usefulness or
# any information, apparatus, product, software, or process disclosed,
# or represents that its use would not infringe privately owned rights.
#
# Reference herein to any specific commercial product, process, or
# service by trade name, trademark, manufacturer, or otherwise does
# not necessarily constitute or imply its endorsement, recommendation,
# r favoring by the United States Government or any agency thereof,
# or Battelle Memorial Institute. The views and opinions of authors
# expressed herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY
# operated by BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830

# }}}
from __future__ import absolute_import, print_function

import logging
import sys
from collections import defaultdict

from crate.client.exceptions import ConnectionError, ProgrammingError
from crate import client
from zmq.utils import jsonapi

from . crate_utils import (create_schema, select_all_topics_query,
                           insert_data_query, insert_topic_query)
from volttron.platform.agent.utils import get_utc_seconds_from_epoch
from volttron.utils.docs import doc_inherit
from volttron.platform.agent import utils
from volttron.platform.agent.base_historian import BaseHistorian


utils.setup_logging()
_log = logging.getLogger(__name__)
__version__ = '1.0'


def historian(config_path, **kwargs):
    """
    This method is called by the :py:func:`crate_historian.historian.main` to parse
    the passed config file or configuration dictionary object, validate the
    configuration entries, and create an instance of MongodbHistorian

    :param config_path: could be a path to a configuration file or can be a
                        dictionary object
    :param kwargs: additional keyword arguments if any
    :return: an instance of :py:class:`CrateHistorian`
    """
    if isinstance(config_path, dict):
        config_dict = config_path
    else:
        config_dict = utils.load_config(config_path)
    connection = config_dict.get('connection', None)
    assert connection is not None

    database_type = connection.get('type', None)
    assert database_type is not None

    params = connection.get('params', None)
    assert params is not None

    topic_replacements = config_dict.get('topic_replace_list', None)
    _log.debug('topic_replacements are: {}'.format(topic_replacements))

    readonly = config_dict.get('readonly', False)

    CrateHistorian.__name__ = 'CrateHistorian'
    return CrateHistorian(config_dict,
                          readonly=readonly,
                          topic_replace_list=topic_replacements,
                          **kwargs)


class CrateHistorian(BaseHistorian):
    """
    Historian that stores the data into crate tables.

    """

    def __init__(self, config, **kwargs):
        """
        Initialize the historian.

        The historian makes a crateclient connection to the crate cluster.
        This connection is thread-safe and therefore we create it before
        starting the main loop of the agent.

        In addition, the topic_map and topic_meta are used for caching meta
        data and topics respectively.

        :param kwargs: additional keyword arguments. (optional identity and
                       topic_replace_list used by parent classes)

        """
        # self.tables_def, table_names = self.parse_table_def(config)
        # self._data_collection = table_names['data_table']
        # self._meta_collection = table_names['meta_table']
        # self._topic_collection = table_names['topics_table']
        # self._agg_topic_collection = table_names['agg_topics_table']
        # self._agg_meta_collection = table_names['agg_meta_table']

        _log.debug(config)
        self._connection_params = config['connection']['params']
        self._schema = config['connection'].get('schema', 'historian')
        self._raw_schema_enabled = config.get('raw_schema_enabled', None)
        self._client = None
        self._connection = None

        self._topic_set = set()

        self._topic_id_map = {}
        self._topic_to_table_map = {}
        self._topic_to_datatype_map = {}
        self._topic_name_map = {}
        self._topic_meta = {}
        self._agg_topic_id_map = {}
        self._initialized = False
        self._wait_until = None
        super(CrateHistorian, self).__init__(**kwargs)

    @doc_inherit
    def publish_to_historian(self, to_publish_list):
        _log.debug("publish_to_historian number of items: {}".format(
            len(to_publish_list)))
        # Verify that we have actually gone through the historian_setup code
        # before we attempt to do anything else.
        if not self._initialized:
            self.historian_setup()
            if not self._initialized:
                return

        if self._wait_until is not None:
            ct = get_utc_seconds_from_epoch()
            if ct > self._wait_until:
                self._wait_until = None
            else:
                _log.debug('Waiting to attempt to write to database.')
                return
        try:
            if self._connection is None:
                self._connection = self.get_connection()

            cursor = self._connection.cursor()

            batch_data = []
            batch_topics = []

            for row in to_publish_list:
                ts = utils.format_timestamp(row['timestamp'])
                source = row['source']
                topic = row['topic']
                value = row['value']
                meta = row['meta']

                if topic not in self._topic_set:
                    batch_topics.append((topic,))

                batch_data.append(
                    (ts, topic, source, value, meta)
                )
                batch_data.append(
                    (ts, topic, source, value, meta)
                )

            if batch_topics:
                _log.debug('Inserting batch topics: {}'.format(batch_topics))
                cursor.executemany(insert_topic_query(self._schema),
                                   batch_topics)

            try:
                query = insert_data_query(self._schema)
                _log.debug("Inserting batch data: {}".format(batch_data))
                cursor.executemany(query, batch_data)
            except ProgrammingError as ex:
                _log.error(
                    "Invalid data detected during batch insert: {}".format(
                        ex.args))
                _log.debug("Attempting singleton insert.")
                insert = insert_data_query(self._schema)
                for id in range(len(batch_data)):
                    try:
                        batch = batch_data[id]
                        cursor.execute(insert, batch)
                    except ProgrammingError:
                        _log.debug('Invalid data not saved {}'.format(
                            to_publish_list[id]
                        ))
                        self.report_handled(to_publish_list[id])
                    except Exception as ex:
                        _log.error(
                            "Exception Type: {} ARGS: {}".format(type(ex),
                                                                 ex.args))
                    else:
                        self.report_handled(to_publish_list[id])

            except Exception as ex:
                _log.error(
                    "Exception Type: {} ARGS: {}".format(type(ex), ex.args))

            else:
                self.report_all_handled()
        except TypeError as ex:
            _log.error(
                "AFTER EXCEPTION: {} ARGS: {}".format(type(ex), ex.args))
        except Exception as ex:
            _log.error(
                "Unknown Exception {} {}".format(type(ex), ex.args)
            )

        finally:
            if cursor is not None:
                cursor.close()
                cursor = None

    @staticmethod
    def _build_single_topic_select_query(start, end, agg_type, agg_period, skip,
                                         count, order, table_name, topic):
        query = """SELECT topic,
                    date_format('%Y-%m-%dT%H:%i:%s.%f+00:00', ts) as ts,
                    coalesce(try_cast(double_value as string), string_value) as result,
                    meta
                        FROM """ + table_name + """
                        {where}
                        {order_by}
                        {limit}
                        {offset}""".replace("\n", "")

        where_clauses = ["WHERE topic =?"]
        args = [topic]
        if start and end and start == end:
            where_clauses.append("ts = ?")
            args.append(start)
        elif start:
            where_clauses.append("ts >= ?")
            args.append(start)
        elif end:
            where_clauses.append("ts < ?")
            args.append(end)

        where_statement = ' AND '.join(where_clauses)

        order_by = 'ORDER BY ts ASC'
        if order == 'LAST_TO_FIRST':
            order_by = ' ORDER BY topic DESC, ts DESC'

        # can't have an offset without a limit
        # -1 = no limit and allows the user to
        # provide just an offset
        if count is None:
            count = 100

        if count > 1000:
            _log.warn("Limiting count to <= 1000")
            count = 1000

        limit_statement = 'LIMIT ?'
        args.append(int(count))

        offset_statement = ''
        if skip > 0:
            offset_statement = 'OFFSET ?'
            args.append(skip)

        real_query = query.format(where=where_statement,
                                  limit=limit_statement,
                                  offset=offset_statement,
                                  order_by=order_by)

        _log.debug("Real Query: " + real_query)
        return real_query, args

    @doc_inherit
    def query_historian(self, topic, start=None, end=None, agg_type=None,
                        agg_period=None, skip=0, count=None,
                        order="FIRST_TO_LAST"):

        # Verify that we have initialized through the historian setup code
        # before we do anything else.
        if not self._initialized:
            self.historian_setup()
            if not self._initialized:
                return {}

        if count is not None:
            try:
                count = int(count)
            except ValueError:
                count = 20
            else:
                # protect the querying of the database limit to 500 at a time.
                if count > 100:
                    count = 100

        # Final results that are sent back to the client.
        results = {}

        # A list or a single topic is now accepted for the topic parameter.
        if not isinstance(topic, list):
            topics = [topic]
        else:
            # Copy elements into topic list
            topics = [x for x in topic]

        values = defaultdict(list)
        metadata = {}
        table_name = "{}.data".format(self._schema)
        cursor = self.get_connection().cursor()

        for topic in topics:
            query, args = self._build_single_topic_select_query(
                start, end, agg_type, agg_period, skip, count, order,
                table_name, topic)

            cursor.execute(query, args)

            for _id, ts, value, meta in cursor.fetchall():
                try:
                    value = float(value)
                except ValueError:
                    pass

                values[topic].append(
                    (
                        utils.format_timestamp(
                            utils.parse_timestamp_string(ts)),
                        value
                    )
                )
                if len(topics) == 1:
                    metadata = meta

        if len(topics) > 1:
            results['values'] = values
            results['metadata'] = {}
        else:  # return the list from the single topic
            results['values'] = values[topics[0]]
            results['metadata'] = metadata

        return results

    @doc_inherit
    def query_topic_list(self):
        _log.debug("Querying topic list")
        cursor = self.get_connection().cursor()
        sql = """
            SELECT name, lower(name)
            FROM {schema}.topic
        """.format(schema=self._schema)

        cursor.execute(sql)

        results = [x[0] for x in cursor.fetchall()]
        return results

    def get_connection(self):
        if self._connection is None:
            self._connection = client.connect(self._connection_params['host'],
                                              error_trace=True)
        return self._connection

    @doc_inherit
    def historian_setup(self):
        try:
            self._connection = self.get_connection()

            _log.debug("Using schema: {}".format(self._schema))
            if not self._readonly:
                create_schema(self._connection, self._schema)

            cursor = self._connection.cursor()
            cursor.execute(select_all_topics_query(self._schema))

            topics = [x[0] for x in cursor.fetchall()]
            self._topic_set = set(topics)
            self._initialized = True
        except Exception as e:
            _log.error("Exception during historian setup!")
            _log.error(e.args)


def main(argv=sys.argv):
    """Main method called by the eggsecutable.
    @param argv:
    """
    try:
        utils.vip_main(historian)
    except Exception as e:
        print(e)
        _log.exception('unhandled exception')


if __name__ == '__main__':
    # Entry point for script
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
