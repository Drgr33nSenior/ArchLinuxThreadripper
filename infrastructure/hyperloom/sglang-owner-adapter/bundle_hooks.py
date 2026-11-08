"""Source-authenticated, exactly-once eager probes inside the selected scheduler."""
from contextlib import contextmanager
from types import FunctionType

from bundle_auth import authenticate_target, _match, _source
import bundle_contract as c


@contextmanager
def worker_scope(target, sources, graph_module, graph_source):
    cls, originals = authenticate_target(target, sources)
    graph_cls = graph_module.DecodeCudaGraphRunner
    original_graph = graph_cls.__dict__.get('can_run_graph')
    _match(original_graph, _source(graph_source), 'DecodeCudaGraphRunner.can_run_graph')
    c.need(original_graph.__globals__ is graph_module.__dict__, 'native graph-admission globals')
    instance = None

    def run(self, batch, *args, **kwargs):
        nonlocal instance
        c.need(type(self) is cls and self.enable_overlap is False and
               'run_batch' not in self.__dict__, 'actual non-overlap Scheduler instance')
        if instance is None:
            instance = self
        c.need(self is instance, 'bundle Scheduler changed')
        value = c.batch_phase(batch, c.CONFIG['probe_ids'])
        with c.phase(value):
            return originals['run_batch'](self, batch, *args, **kwargs)

    def can_run(self, forward_batch):
        c.need(type(self) is graph_cls and 'can_run_graph' not in self.__dict__,
               'actual graph-admission instance')
        if c.PHASE is not None:
            return False
        return original_graph(self, forward_batch)

    cls.run_batch = run
    graph_cls.can_run_graph = can_run
    try:
        yield
    finally:
        foreign = (cls.__dict__.get('run_batch') is not run or
                   graph_cls.__dict__.get('can_run_graph') is not can_run)
        if cls.__dict__.get('run_batch') is run:
            cls.run_batch = originals['run_batch']
        if graph_cls.__dict__.get('can_run_graph') is can_run:
            graph_cls.can_run_graph = original_graph
        # Do not replace the original failure or interrupt with cleanup wording.
        import sys
        if foreign and sys.exc_info()[1] is None:
            raise ValueError('bundle hook replaced by foreign code')
