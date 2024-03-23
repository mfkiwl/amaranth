from contextlib import contextmanager
import itertools
import re

from ..hdl import *
from ..hdl._repr import *
from ..hdl._mem import MemoryInstance, MemoryIdentity
from ..hdl._ast import SignalDict, Slice, Operator
from ._base import *
from ._pyrtl import _FragmentCompiler
from ._pycoro import PyCoroProcess
from ._pyclock import PyClockProcess


__all__ = ["PySimEngine"]


class _VCDWriter:
    @staticmethod
    def decode_to_vcd(format, value):
        return format.format(value).expandtabs().replace(" ", "_")

    @staticmethod
    def eval_field(field, signal, value):
        if isinstance(field, Signal):
            assert field is signal
            return value
        elif isinstance(field, Const):
            return field.value
        elif isinstance(field, Slice):
            sub = _VCDWriter.eval_field(field.value, signal, value)
            return (sub >> field.start) & ((1 << (field.stop - field.start)) - 1)
        elif isinstance(field, Operator) and field.operator in ('s', 'u'):
            sub = _VCDWriter.eval_field(field.operands[0], signal, value)
            return Const(sub, field.shape()).value
        else:
            raise NotImplementedError

    def __init__(self, design, *, vcd_file, gtkw_file=None, traces=()):
        # Although pyvcd is a mandatory dependency, be resilient and import it as needed, so that
        # the simulator is still usable if it's not installed for some reason.
        import vcd, vcd.gtkw

        self.close_vcd = False
        self.close_gtkw = False
        if isinstance(vcd_file, str):
            vcd_file = open(vcd_file, "w")
            self.close_vcd = True
        if isinstance(gtkw_file, str):
            gtkw_file = open(gtkw_file, "w")
            self.close_gtkw = True

        self.vcd_signal_vars = SignalDict()
        self.vcd_memory_vars = {}
        self.vcd_file = vcd_file
        self.vcd_writer = vcd_file and vcd.VCDWriter(self.vcd_file,
            timescale="1 ps", comment="Generated by Amaranth")

        self.gtkw_signal_names = SignalDict()
        self.gtkw_memory_names = {}
        self.gtkw_file = gtkw_file
        self.gtkw_save = gtkw_file and vcd.gtkw.GTKWSave(self.gtkw_file)

        self.traces = []

        signal_names = SignalDict()
        memories = {}
        for fragment, fragment_info in design.fragments.items():
            fragment_name = ("bench", *fragment_info.name)
            for signal, signal_name in fragment_info.signal_names.items():
                if signal not in signal_names:
                    signal_names[signal] = set()
                signal_names[signal].add((*fragment_name, signal_name))
            if isinstance(fragment, MemoryInstance):
                memories[fragment._identity] = (fragment, fragment_name)

        trace_names = SignalDict()
        assigned_names = set()
        for trace in traces:
            if isinstance(trace, ValueLike):
                trace = Value.cast(trace)
                for trace_signal in trace._rhs_signals():
                    if trace_signal not in signal_names:
                        if trace_signal.name not in assigned_names:
                            name = trace_signal.name
                        else:
                            name = f"{trace_signal.name}${len(assigned_names)}"
                            assert name not in assigned_names
                        trace_names[trace_signal] = {("bench", name)}
                        assigned_names.add(name)
                    self.traces.append(trace_signal)
            elif hasattr(trace, "_identity") and isinstance(trace._identity, MemoryIdentity):
                if not trace._identity in memories:
                    raise ValueError(f"{trace!r} is a memory not part of the elaborated design")
                self.traces.append(trace._identity)
            else:
                raise TypeError(f"{trace!r} is not a traceable object")

        if self.vcd_writer is None:
            return

        for signal, names in itertools.chain(signal_names.items(), trace_names.items()):
            self.vcd_signal_vars[signal] = []
            self.gtkw_signal_names[signal] = []
            for repr in signal._value_repr:
                var_init = self.eval_field(repr.value, signal, signal.init)
                if isinstance(repr.format, FormatInt):
                    var_type = "wire"
                    var_size = repr.value.shape().width
                else:
                    var_type = "string"
                    var_size = 1
                    var_init = self.decode_to_vcd(repr.format, var_init)

                vcd_var = None
                for (*var_scope, var_name) in names:
                    if re.search(r"[ \t\r\n]", var_name):
                        raise NameError("Signal '{}.{}' contains a whitespace character"
                                        .format(".".join(var_scope), var_name))

                    field_name = var_name
                    for item in repr.path:
                        if isinstance(item, int):
                            field_name += f"[{item}]"
                        else:
                            field_name += f".{item}"
                    if repr.path:
                        field_name = "\\" + field_name

                    if vcd_var is None:
                        vcd_var = self.vcd_writer.register_var(
                            scope=var_scope, name=field_name,
                            var_type=var_type, size=var_size, init=var_init)
                        if var_size > 1:
                            suffix = f"[{var_size - 1}:0]"
                        else:
                            suffix = ""
                        self.gtkw_signal_names[signal].append(".".join((*var_scope, field_name)) + suffix)
                    else:
                        self.vcd_writer.register_alias(
                            scope=var_scope, name=field_name,
                            var=vcd_var)

                self.vcd_signal_vars[signal].append((vcd_var, repr))

        for memory, memory_name in memories.values():
            self.vcd_memory_vars[memory._identity] = vcd_vars = []
            self.gtkw_memory_names[memory._identity] = gtkw_names = []
            if memory._width > 1:
                suffix = f"[{memory._width - 1}:0]"
            else:
                suffix = ""
            for idx, init in enumerate(memory._init):
                field_name = "\\" + memory_name[-1] + f"[{idx}]"
                var_scope = memory_name[:-1]
                vcd_var = self.vcd_writer.register_var(
                    scope=var_scope, name=field_name,
                    var_type="wire", size=memory._width, init=init,
                )
                vcd_vars.append(vcd_var)
                gtkw_field_name = field_name + suffix
                gtkw_name = ".".join((*var_scope, gtkw_field_name))
                gtkw_names.append(gtkw_name)


    def update_signal(self, timestamp, signal, value):
        for (vcd_var, repr) in self.vcd_signal_vars.get(signal, ()):
            var_value = self.eval_field(repr.value, signal, value)
            if not isinstance(repr.format, FormatInt):
                var_value = self.decode_to_vcd(repr.format, var_value)
            self.vcd_writer.change(vcd_var, timestamp, var_value)

    def update_memory(self, timestamp, memory, addr, value):
        vcd_var = self.vcd_memory_vars[memory._identity][addr]
        self.vcd_writer.change(vcd_var, timestamp, value)

    def close(self, timestamp):
        if self.vcd_writer is not None:
            self.vcd_writer.close(timestamp)

        if self.gtkw_save is not None:
            self.gtkw_save.dumpfile(self.vcd_file.name)
            self.gtkw_save.dumpfile_size(self.vcd_file.tell())

            self.gtkw_save.treeopen("top")
            for signal in self.traces:
                if isinstance(signal, Signal):
                    for name in self.gtkw_signal_names[signal]:
                        self.gtkw_save.trace(name)
                elif isinstance(signal, MemoryIdentity):
                    for name in self.gtkw_memory_names[signal]:
                        self.gtkw_save.trace(name)
                else:
                    assert False # :nocov:

        if self.close_vcd:
            self.vcd_file.close()
        if self.close_gtkw:
            self.gtkw_file.close()


class _Timeline:
    def __init__(self):
        self.now = 0
        self.deadlines = dict()

    def reset(self):
        self.now = 0
        self.deadlines.clear()

    def at(self, run_at, process):
        assert process not in self.deadlines
        self.deadlines[process] = run_at

    def delay(self, delay_by, process):
        if delay_by is None:
            run_at = self.now
        else:
            run_at = self.now + delay_by
        self.at(run_at, process)

    def advance(self):
        nearest_processes = set()
        nearest_deadline = None
        for process, deadline in self.deadlines.items():
            if deadline is None:
                if nearest_deadline is not None:
                    nearest_processes.clear()
                nearest_processes.add(process)
                nearest_deadline = self.now
                break
            elif nearest_deadline is None or deadline <= nearest_deadline:
                assert deadline >= self.now
                if nearest_deadline is not None and deadline < nearest_deadline:
                    nearest_processes.clear()
                nearest_processes.add(process)
                nearest_deadline = deadline

        if not nearest_processes:
            return False

        for process in nearest_processes:
            process.runnable = True
            del self.deadlines[process]
        self.now = nearest_deadline

        return True


class _PySignalState(BaseSignalState):
    __slots__ = ("signal", "curr", "next", "waiters", "pending")

    def __init__(self, signal, pending):
        self.signal = signal
        self.pending = pending
        self.waiters = {}
        self.curr = self.next = signal.init

    def set(self, value):
        if self.next == value:
            return
        self.next = value
        self.pending.add(self)

    def commit(self):
        if self.curr == self.next:
            return False
        self.curr = self.next

        awoken_any = False
        for process, trigger in self.waiters.items():
            if trigger is None or trigger == self.curr:
                process.runnable = awoken_any = True
        return awoken_any


class _PyMemoryChange:
    __slots__ = ("state", "addr")

    def __init__(self, state, addr):
        self.state = state
        self.addr = addr


class _PyMemoryState(BaseMemoryState):
    __slots__ = ("memory", "data", "write_queue", "waiters", "pending")

    def __init__(self, memory, pending):
        self.memory = memory
        self.pending = pending
        self.waiters = {}
        self.reset()

    def reset(self):
        self.data = list(self.memory._init)
        self.write_queue = []

    def commit(self):
        if not self.write_queue:
            return False

        for addr, value, mask in self.write_queue:
            curr = self.data[addr]
            value = (value & mask) | (curr & ~mask)
            self.data[addr] = value
        self.write_queue.clear()

        awoken_any = False
        for process in self.waiters:
            process.runnable = awoken_any = True
        return awoken_any

    def read(self, addr):
        if addr not in range(self.memory._depth):
            return 0

        return self.data[addr]

    def write(self, addr, value, mask=None):
        if addr not in range(self.memory._depth):
            return
        if mask == 0:
            return

        if mask is None:
            mask = (1 << self.memory._width) - 1

        self.write_queue.append((addr, value, mask))
        self.pending.add(self)


class _PySimulation(BaseSimulation):
    def __init__(self):
        self.timeline  = _Timeline()
        self.signals   = SignalDict()
        self.memories  = {}
        self.slots     = []
        self.pending   = set()

    def add_memory(self, fragment):
        self.memories[fragment._identity] = len(self.slots)
        self.slots.append(_PyMemoryState(fragment, self.pending))

    def reset(self):
        self.timeline.reset()
        for signal, index in self.signals.items():
            state = self.slots[index]
            assert isinstance(state, _PySignalState)
            state.curr = state.next = signal.init
        for index in self.memories.values():
            state = self.slots[index]
            assert isinstance(state, _PyMemoryState)
            state.reset()
        self.pending.clear()

    def get_signal(self, signal):
        try:
            return self.signals[signal]
        except KeyError:
            index = len(self.slots)
            self.slots.append(_PySignalState(signal, self.pending))
            self.signals[signal] = index
            return index

    def add_trigger(self, process, signal, *, trigger=None):
        index = self.get_signal(signal)
        assert (process not in self.slots[index].waiters or
                self.slots[index].waiters[process] == trigger)
        self.slots[index].waiters[process] = trigger

    def remove_trigger(self, process, signal):
        index = self.get_signal(signal)
        assert process in self.slots[index].waiters
        del self.slots[index].waiters[process]

    def add_memory_trigger(self, process, identity):
        index = self.memories[identity]
        self.slots[index].waiters[process] = None

    def remove_memory_trigger(self, process, identity):
        index = self.memories[identity]
        assert process in self.slots[index].waiters
        del self.slots[index].waiters[process]

    def wait_interval(self, process, interval):
        self.timeline.delay(interval, process)

    def commit(self, changed=None):
        converged = True
        for state in self.pending:
            if changed is not None:
                if isinstance(state, _PyMemoryState):
                    for addr, _value, _mask in state.write_queue:
                        changed.add(_PyMemoryChange(state, addr))
                elif isinstance(state, _PySignalState):
                    changed.add(state)
                else:
                    assert False # :nocov:
            if state.commit():
                converged = False
        self.pending.clear()
        return converged


class PySimEngine(BaseEngine):
    def __init__(self, design):
        self._state = _PySimulation()
        self._timeline = self._state.timeline

        self._design = design
        self._processes = _FragmentCompiler(self._state)(self._design.fragment)
        self._testbenches = []
        self._vcd_writers = []

    def add_clock_process(self, clock, *, phase, period):
        self._processes.add(PyClockProcess(self._state, clock,
                                           phase=phase, period=period))

    def add_coroutine_process(self, process, *, default_cmd):
        self._processes.add(PyCoroProcess(self._state, self._design.fragment.domains, process,
                                          default_cmd=default_cmd))

    def add_testbench_process(self, process):
        self._testbenches.append(PyCoroProcess(self._state, self._design.fragment.domains, process,
                                               testbench=True))

    def reset(self):
        self._state.reset()
        for process in self._processes:
            process.reset()

    def _step_rtl(self, changed):
        # Performs the two phases of a delta cycle in a loop:
        converged = False
        while not converged:
            # 1. eval: run and suspend every non-waiting process once, queueing signal changes
            for process in self._processes:
                if process.runnable:
                    process.runnable = False
                    process.run()

            # 2. commit: apply every queued signal change, waking up any waiting processes
            converged = self._state.commit(changed)

    def _step_tb(self):
        changed = set() if self._vcd_writers else None

        # Run processes waiting for an interval to expire (mainly `add_clock_process()``)
        self._step_rtl(changed)

        # Run testbenches waiting for an interval to expire, or for a signal to change state
        converged = False
        while not converged:
            converged = True
            # Schedule testbenches in a deterministic, predictable order by iterating a list
            for testbench in self._testbenches:
                if testbench.runnable:
                    testbench.runnable = False
                    while testbench.run():
                        # Testbench has changed simulation state; run processes triggered by that
                        converged = False
                        self._step_rtl(changed)

        for vcd_writer in self._vcd_writers:
            for change in changed:
                if isinstance(change, _PySignalState):
                    signal_state = change
                    vcd_writer.update_signal(self._timeline.now,
                        signal_state.signal, signal_state.curr)
                elif isinstance(change, _PyMemoryChange):
                    vcd_writer.update_memory(self._timeline.now, change.state.memory,
                                             change.addr, change.state.data[change.addr])
                else:
                    assert False # :nocov:

    def advance(self):
        self._step_tb()
        self._timeline.advance()
        return any(not process.passive for process in (*self._processes, *self._testbenches))

    @property
    def now(self):
        return self._timeline.now

    @contextmanager
    def write_vcd(self, *, vcd_file, gtkw_file, traces):
        vcd_writer = _VCDWriter(self._design,
            vcd_file=vcd_file, gtkw_file=gtkw_file, traces=traces)
        try:
            self._vcd_writers.append(vcd_writer)
            yield
        finally:
            vcd_writer.close(self._timeline.now)
            self._vcd_writers.remove(vcd_writer)
