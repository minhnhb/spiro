#!/usr/bin/env python

import logging
import os
import mongoengine
import settings
from datetime import timedelta
import tornado.ioloop
import tornado.httpserver
import tornado.web
from tornado import gen
from tornado.options import define, options

define("debug", default=False, help="run in debug mode", type=bool)
define("prefork", default=False, help="pre-fork across all CPUs", type=bool)
define("port", default=9000, help="run on the given port", type=int)
define("bootstrap", default=False, help="Run the bootstrap model commands")

from spiro.web.route import route
from spiro.web.main import RedirectHandler
from spiro.metrics import Metrics
from spiro.queue import RedisQueue
from spiro.pipeline import Pipeline
from spiro import redis
from spiro import models

#
#
#
class Application(tornado.web.Application):
    def __init__(self):
        if options.debug:
            logging.getLogger().setLevel(logging.DEBUG)

        app_settings = dict(
            debug=options.debug,
            template_path=os.path.join(os.path.dirname(__file__), "web", "templates"),
            static_path=os.path.join(os.path.dirname(__file__), "web", "static"),
        )

        mongoengine.connect(settings.STORE_BUCKET)

        self.user_settings = models.Settings.singleton()
        self.redis = redis.Client()
        self.redis.connect()
        self.work_queue = RedisQueue(self.redis)
        self.work_queue.default_delay = self.user_settings.crawl_delay
        self.metrics = Metrics()

        routes = route.get_routes()
        # Hast to be the last route...
        routes.extend([
                    (r"/(.+)", RedirectHandler),
                ])

        super(Application, self).__init__(routes, **app_settings)


        self.ioloop  = tornado.ioloop.IOLoop.instance()

        self.fetchers = []
        self.set_fetchers(self.user_settings.max_fetchers)

    def set_fetchers(self, count):
        print "SETTING POOL SIZE", count, len(self.fetchers)

        while count < len(self.fetchers):
            fetcher = self.fetchers.pop()
            fetcher.stop()

        while len(self.fetchers) < count:
            self.fetchers.append(Worker(self, settings, self.work_queue))

class Worker(object):
    def __init__(self, app, settings, queue, io_loop=None):
        self.app      = app
        self.user_settings = models.Settings.singleton()
        self.ioloop   = io_loop or tornado.ioloop.IOLoop.instance()
        self.queue    = queue
        self.pipeline = Pipeline(settings.PIPELINE, settings=settings, work_queue=queue, user_settings=app.user_settings)
        self.running_fetchers  = 0
        self.total_fetch_count = 0
        self._stopping = False
        self.ioloop.add_callback(self.loop)

    @gen.engine
    def loop(self):
        if self._stopping:
            return

        if not self.queue or not self.app.user_settings.crawler_running:
            self.ioloop.add_timeout(timedelta(seconds=1), self.loop)
            return

        task = None
        try:
            task, complete_cb = yield gen.Task(self.queue.pop)
        except Exception as e:
            pass

        if not task:
            self.ioloop.add_timeout(timedelta(seconds=self.user_settings.crawl_delay), self.loop)
            return

        logging.debug("Staring task url=%s" % task.url)

        self.running_fetchers  += 1

        yield gen.Task(self.pipeline.process, task)
        complete_cb(True, task)

        self.app.metrics.add('response:%s' % task.url_host, task.response.request_time)

        self.total_fetch_count += 1
        self.running_fetchers  -= 1

        if task.response:
            models.LogEvent("Crawled %d %s" % (task.response.code, task.url)).save()
        else:
            models.LogEvent("NOT Crawled %s" % (task.url)).save()
        logging.debug("Finished task url=%s" % task.url)

        self.ioloop.add_callback(self.loop)

    def stop(self):
        self._stopping = True

def application():
    tornado.options.parse_command_line()

    #queue    = SpiderQueue()
    app      = Application()

    http_server = tornado.httpserver.HTTPServer(app)

    print "Starting tornado on port", options.port
    if options.prefork:
        print "\tpre-forking"
        http_server.bind(options.port)
        http_server.start()
    else:
        http_server.listen(options.port)

    try:
        tornado.ioloop.IOLoop.instance().start()
    except KeyboardInterrupt:
        pass

def main():
    application()

if __name__ == '__main__':
    main()
