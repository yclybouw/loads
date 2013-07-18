import datetime

from tornado import ioloop, gen

from loads.util import resolve_name, logger
from loads.test_result import TestResult
from loads.relay import ZMQRelay
from loads.output import create_output


def _compute_arguments(args):
    """
    Read the given :param args: and builds up the total number of runs, the
    number of cycles, duration, users and agents to use.

    Returns a tuple of (total, cycles, duration, users, agents).
    """
    users = args.get('users', '1')
    if isinstance(users, str):
        users = users.split(':')
    users = [int(user) for user in users]
    cycles = args.get('cycles')
    duration = args.get('duration')
    if duration is None and cycles is None:
        cycles = '1'

    if cycles is not None:
        if not isinstance(cycles, list):
            cycles = [int(cycle) for cycle in cycles.split(':')]

    agents = args.get('agents', 1)

    # XXX duration based == no total
    total = 0
    if duration is None:
        for user in users:
            total += sum([cycle * user for cycle in cycles])
        if agents is not None:
            total *= agents

    return total, cycles, duration, users, agents


class Runner(object):
    """Local tests runner.

    Runs in parallel a number of tests and pass the results to the outputs.

    It can be run in two different modes:

    - "Classical" mode: Results are collected and passed to the outputs.
    - "Slave" mode: Results are sent to a ZMQ endpoint and no output is called.
    """
    def __init__(self, args):
        self.args = args
        self.fqn = args['fqn']
        self.test = resolve_name(self.fqn)
        self.slave = 'slave' in args
        self.outputs = []
        self._stopped = False

        self.loop = ioloop.IOLoop()

        (self.total, self.cycles,
         self.duration, self.users, self.agents) = _compute_arguments(args)

        self.args['cycles'] = self.cycles
        self.args['users'] = self.users
        self.args['agents'] = self.agents
        self.args['total'] = self.total

        # If we are in slave mode, set the test_result to a 0mq relay
        if self.slave:
            self.test_result = ZMQRelay(self.args)

        # The normal behavior is to collect the results locally.
        else:
            self.test_result = TestResult(args=self.args)

        if not self.slave:
            for output in self.args.get('output', ['stdout']):
                self.register_output(output)

    def register_output(self, output_name):
        output = create_output(output_name, self.test_result, self.args)
        self.outputs.append(output)
        self.test_result.add_observer(output)

    def execute(self):
        self.loop.add_callback(self._execute)
        self.loop.start()
        if (not self.slave and
                self.test_result.nb_errors + self.test_result.nb_failures):
            return 1
        return 0

    @gen.coroutine
    def _execute(self):
        """Spawn all the tests needed and wait for them to finish.
        """
        # Check that self.test is right before doing anything else.
        if not hasattr(self.test, 'im_class'):
            raise ValueError(self.test)

        try:
            worker_id = self.args.get('worker_id', None)

            # Refresh the outputs every so often.
            cb = ioloop.PeriodicCallback(self.refresh, 100, self.loop)
            cb.start()

            self.test_result.startTestRun(worker_id)

            if self.duration is not None:
                # Be sure to stop our loop after a certain time.
                logger.info('Running the tests for %ss.' % self.duration)

                duration_delta = datetime.timedelta(seconds=self.duration)
                self.loop.add_timeout(duration_delta, self.stop)

            runs = []
            for user in self.users:
                for i in range(user):
                    runs.append((i, user))
            from pdb import set_trace; set_trace()
            yield [gen.Task(self._run_test, num, user) for (num, user) in runs]

        except KeyboardInterrupt:
            pass  # It's possible to stop the tests with ctrl-c.
        finally:
            self.stop()

    @gen.coroutine
    def _run_test(self, num, user, cycle=1):

        # Create a test case instance for each virtual user.
        test = self.test.im_class(test_name=self.test.__name__,
                                  test_result=self.test_result,
                                  config=self.args)

        if self.duration:
            # We want to run tests until someone stop us.
            test(loads_status=(0, user, cycle, num))
            self.loop.add_callback(self._run_test, num, user, cycle + 1)
        else:
            tasks = []
            for cycle in self.cycles:
                for current_cycle in range(cycle):
                    tasks.append((cycle, user, current_cycle + 1, num))

            yield [gen.Task(test, loads_status=t) for t in tasks]

    def stop(self):
        if not self._stopped:
            self.loop.stop()
            if not self.slave:
                self.flush()
            else:
                # in slave mode, be sure to close the zmq relay.
                self.test_result.close()
            self._stopped = True

    def flush(self):
        for output in self.outputs:
            if hasattr(output, 'flush'):
                output.flush()

    def refresh(self):
        for output in self.outputs:
            if hasattr(output, 'refresh'):
                output.refresh()
