import operator
import unittest
import contextlib
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.testing import FileCheck

# these needs to be set before `common_utils`
# infers `GRAPH_EXECUTOR`.
# this file **requires** these settings
# and setting them after `GRAPH_EXECUTOR` is
# inferred erroneously runs or skips
# some tests
torch._C._jit_set_profiling_executor(True)
torch._C._jit_set_profiling_mode(True)

from torch.testing._internal.common_utils import run_tests, ProfilingMode, GRAPH_EXECUTOR, \
    enable_profiling_mode_for_profiling_tests
from torch.testing._internal.jit_utils import JitTestCase, _inline_everything, \
    RUN_CUDA, RUN_CUDA_HALF, RUN_CUDA_MULTI_GPU, warmup_backward, set_fusion_group_inlining

from textwrap import dedent
from itertools import product, permutations

from test_jit import backward_graph, all_backward_graphs, get_lstm_inputs, get_milstm_inputs, \
    LSTMCellC, LSTMCellF, LSTMCellS, MiLSTMCell

from torch.testing._internal.te_utils import CudaCodeGenExecuted

FUSION_GROUP = 'prim::TensorExprGroup'
LLVM_ENABLED = torch._C._llvm_enabled()

def strip_profiling_nodes(nodes):
    profiling_opcodes = set(['prim::BailoutTemplate', 'prim::BailOut'])
    return [n for n in nodes if n.kind() not in profiling_opcodes]

def warmup_forward(f, *args):
    profiling_count = 2
    for i in range(profiling_count):
        results = f(*args)

    return results

@contextlib.contextmanager
def texpr_reductions_enabled():
    old = torch._C._jit_set_texpr_reductions_enabled(True)
    try:
        yield
    finally:
        torch._C._jit_set_texpr_reductions_enabled(old)

class TestTEFuser(JitTestCase):
    def setUp(self):
        self.old_cpu_fuser_state = torch._C._jit_can_fuse_on_cpu()
        self.old_must_use_cpu_state = torch._C._jit_get_te_must_use_llvm_cpu()
        self.old_gpu_fuser_state = torch._C._jit_can_fuse_on_gpu()

        torch._C._jit_override_can_fuse_on_cpu(True)
        # TODO: force LLVM. need to add it to asan, mac, windows builds + sandcastle
        # torch._C._jit_set_te_must_use_llvm_cpu(True)
        torch._C._jit_override_can_fuse_on_gpu(True)

        self.old_profiling_executor = torch._C._jit_set_profiling_executor(True)
        self.old_profiling_mode = torch._C._jit_set_profiling_mode(True)

        self.old_fusion_inlining = torch._C._debug_get_fusion_group_inlining()
        torch._C._debug_set_fusion_group_inlining(False)

        self.texpr_fuser_state = torch._C._jit_texpr_fuser_enabled()
        torch._C._jit_set_texpr_fuser_enabled(True)

        self.devices = ['cpu'] if not torch.cuda.is_available() else ['cpu', 'cuda']

    def tearDown(self):
        torch._C._jit_set_profiling_executor(self.old_profiling_executor)
        torch._C._jit_set_profiling_mode(self.old_profiling_mode)

        torch._C._jit_override_can_fuse_on_gpu(self.old_gpu_fuser_state)
        torch._C._jit_override_can_fuse_on_cpu(self.old_cpu_fuser_state)
        torch._C._jit_set_te_must_use_llvm_cpu(self.old_must_use_cpu_state)
        torch._C._debug_set_fusion_group_inlining(self.old_fusion_inlining)

        torch._C._jit_set_texpr_fuser_enabled(self.texpr_fuser_state)

    def assertLastGraphAllFused(self):
        self.assertAllFused(torch.jit.last_executed_optimized_graph())

    def findFusionGroups(self, graph):
        result = []
        for n in graph.nodes():
            if n.kind() == FUSION_GROUP:
                result.append(n.g('Subgraph'))
                continue
            for block in n.blocks():
                result += self.findFusionGroups(block)
        return result

    def _test_fused_abs(self, device='cpu'):
        def func(x):
            return x.abs() * 2

        a = torch.randn(5, device=device)
        scripted = self.checkScript(func, (a,))
        self.assertLastGraphAllFused()

    def test_sum_simple(self):
        def func(x):
            x2 = x * x
            return x2.sum()

        with texpr_reductions_enabled():
            a = torch.tensor(list(x for x in range(0, 15)), dtype=torch.float, device='cpu')
            a = a.reshape(5, 3)
            scripted = self.checkScript(func, (a,))
            self.assertLastGraphAllFused()

    def test_sum_dim(self):
        def func(x):
            return x.sum((0, )) * 2

        def func_neg(x):
            return x.sum((-2, )) * 2

        with texpr_reductions_enabled():
            a = torch.tensor(list(x for x in range(0, 15)), dtype=torch.float, device='cpu')
            a = a.reshape(5, 3)
            scripted = self.checkScript(func, (a,))
            self.assertLastGraphAllFused()
            scripted = self.checkScript(func_neg, (a,))
            self.assertLastGraphAllFused()

    def test_sum_keepdim_cast(self):
        def func(x):
            return x.sum((0, ), keepdim=True, dtype=torch.double) * 2

        with texpr_reductions_enabled():
            a = torch.tensor(list(x for x in range(0, 15)), dtype=torch.float, device='cpu')
            a = a.reshape(5, 3)

            self.checkScript(func, (a,))
            self.assertLastGraphAllFused()

    def test_abs_cpu(self):
        self._test_fused_abs()

    @unittest.skipIf(not RUN_CUDA, "requires CUDA")
    def test_abs_cuda(self):
        self._test_fused_abs(device="cuda")

    def _test_zero_element_tensors(self, device="cpu"):
        def decode(sin_t, cos_t):
            theta = torch.atan2(sin_t.float(), cos_t.float())
            return theta

        sin = torch.zeros(0, device=device)
        cos = torch.zeros(0, device=device)
        inputs = [sin, cos]
        ge = self.checkScript(decode, inputs)

    @unittest.skipIf(not RUN_CUDA, "requires CUDA")
    def test_zero_element_tensors_cuda(self):
        self._test_zero_element_tensors(device="cuda")

    def test_zero_element_tensors_cpu(self):
        self._test_zero_element_tensors(device="cpu")

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_arg_configurations_smoke_cuda(self):
        # A smoke test to make sure we won't use the same kernel for contiguous
        # and non-contiguous arguments.
        # TODO: add optionally enabled debug counters to the fuser to verify
        #       that we really can tell the difference between configurations
        def f(x, y):
            z1, z2 = (x + y).chunk(2, dim=1)
            return z1 * z2

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')
        traced_f = torch.jit.trace(f, (x, y,))
        self.assertEqual(traced_f(x.t().contiguous(), y), traced_f(x.t(), y))

    def test_broadcast(self):
        for device in self.devices:
            def scaleshift(x, scale, shift):
                return x * scale + shift

            inputs = [
                torch.randn(4, 4, dtype=torch.float, device=device),
                torch.randn(4, dtype=torch.float, device=device),
                torch.randn(4, dtype=torch.float, device=device),
            ]
            self.checkScript(scaleshift, inputs)

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skipIf(not RUN_CUDA_HALF, "no half support")
    @unittest.skipIf(GRAPH_EXECUTOR != ProfilingMode.LEGACY, "no half support with profiling on")
    def test_cuda_half(self):
        x = torch.randn(4, 4, dtype=torch.half, device='cuda')
        y = torch.randn(4, 4, dtype=torch.half, device='cuda')

        funcs = [
            self.fn_test_comparison_gt_lt,
            self.fn_test_relu,
            self.fn_test_exp
        ]

        # Note: Non fused inputs must be float to prevent loss of precision
        inputs = (x.float(), y.float())
        fusion_inputs = (x, y)
        for fn in funcs:
            local_inputs = [t.clone().requires_grad_() for t in inputs]
            local_fusion_inputs = [t.clone().requires_grad_() for t in fusion_inputs]

            # Verifies outputs
            fusion = torch.jit.trace(fn, local_fusion_inputs, check_trace=False)
            outputs = fn(*local_inputs)
            fusion_outputs = fusion(*local_fusion_inputs)
            outputs_half = [t.half() for t in outputs]
            self.assertEqual(outputs_half, fusion_outputs)

            # Verifies gradients
            for output, fusion_output in zip(outputs_half, fusion_outputs):
                grads = torch.autograd.grad(
                    output.float().sum(), local_inputs, allow_unused=True, retain_graph=True)
                fusion_grads = torch.autograd.grad(
                    fusion_output.sum(), local_fusion_inputs, allow_unused=True, retain_graph=True)
                grads_half = [t.half() for t in grads]
                self.assertEqual(grads_half, fusion_grads)

    def test_checks_cat_inputs(self):
        # single fusion node causes error
        with set_fusion_group_inlining(True):
            for device in self.devices:
                # We shouldn't treat cat nodes as broadcasting. All their inputs
                # need to be checked for having the same map size, before we can
                # run the kernel.
                def f(x, y):
                    return torch.cat([x + 2 * x + x ** 2, y + 4 * y + y ** 3], dim=0)

                # NOTE: y is broadcastable to x, but output of f(x, y) should have
                # shape 3x4, and not 4x4.
                x = torch.randn(2, 4, dtype=torch.float, device=device)
                y = torch.randn(1, 4, dtype=torch.float, device=device)

                scripted = self.checkScript(f, (x, y))
                self.assertEqual(scripted(x, y).shape, (3, 4))
                self.assertAllFused(scripted.graph_for(x, y))

    def test_chunk(self):
        for device in self.devices:
            def fn(x):
                a, b, c = x.chunk(3, 1)
                return a * b + c

            inputs = [torch.randn(10, 6, dtype=torch.float, device=device)]

            self.checkScript(fn, inputs)
            self.assertLastGraphAllFused()

    @staticmethod
    def _test_chunk_correctness(self, device='cpu'):
        def chunk_4_0(x):
            x0, x1, x2, x3 = x.chunk(4, 0)
            return x0 + x1 + x2 + x3

        def chunk_4_1(x):
            x0, x1, x2, x3 = x.chunk(4, 1)
            return x0 + x1 + x2 + x3

        def chunk_4_last(x):
            x0, x1, x2, x3 = x.chunk(4, 2)
            return x0 + x1 + x2 + x3

        fns = [chunk_4_0, chunk_4_1, chunk_4_last]
        tensors = [
            # splitSize = 1
            torch.randn(4, 4, 4, dtype=torch.float, device=device),

            # contiguous case
            torch.randn(12, 8, 16, dtype=torch.float, device=device),

            # non-contiguous case
            torch.randn(12, 8, 16, dtype=torch.float, device=device).transpose(1, 2),
        ]

        for tensor in tensors:
            for fn in fns:
                self.checkScript(fn, [tensor])
                self.assertLastGraphAllFused()

    def test_chunk_correctness(self):
        return self._test_chunk_correctness(self, 'cpu')

    @unittest.skipIf(not RUN_CUDA, "No CUDA")
    def test_chunk_correctness_cuda(self):
        return self._test_chunk_correctness(self, 'cuda')

    def test_chunk_distributes(self):
        for device in self.devices:
            def f(x, y):
                z1, z2 = (x + y).chunk(2, dim=1)
                return z1 * z2

            x = torch.randn(4, 4, dtype=torch.float, device=device)
            y = torch.randn(4, 4, dtype=torch.float, device=device)

            ge = self.checkTrace(f, (x, y))
            graph = ge.graph_for(x, y)
            # XXX: The old fuser does broadcast_tensors but the new fuser doesn't.
            # FileCheck().check("broadcast_tensors").check('with ' + FUSION_GROUP + '_') \
            #     .check_count('ConstantChunk', 2, exactly=True).run(str(graph))
            FileCheck().check("with " + FUSION_GROUP + "_").check_count(
                "ConstantChunk", 1, exactly=True
            ).run(str(graph))

    def test_chunk_motion_deduplicates_inputs(self):
        for device in self.devices:
            def func1(x):
                z = x * x
                z0, z1 = z.chunk(2)
                return z0 * z1

            def func2(x):
                z = x * x * x
                z0, z1 = z.chunk(2)
                return z0 * z1

            inputs = [
                torch.tensor([1.1, 1.2], device=device, dtype=torch.float),
            ]
            for func in [func1, func2]:
                self.checkScript(func, inputs)
                self.assertLastGraphAllFused()

    def test_chunk_multiple(self):
        for device in self.devices:
            # The arguments are intentionally used out of order as a test to see
            # if the fusion compiler adds extra args in the correct order
            def fn(s, x, y, z):
                z1, z2 = z.chunk(2, 2)
                x1, x2, x3 = x.chunk(3, 1)
                y1, y2 = y.chunk(2, 0)
                return s + x1 + x2 + x3 + y1 + y2 + z1 + z2

            inputs = [
                torch.randn(5, 2, 3, dtype=torch.float, device=device),
                torch.randn(5, 6, 3, dtype=torch.float, device=device),
                torch.randn(10, 2, 3, dtype=torch.float, device=device),
                torch.randn(5, 2, 6, dtype=torch.float, device=device),
            ]

            ge = self.checkScript(fn, inputs)
            self.assertAllFused(ge.graph_for(*inputs))

    def test_minmax(self):
        for device in self.devices:
            def tmax(a, b):
                return torch.max(2 * a, b)

            def tmin(a, b):
                return torch.min(2 * a, b)

            a = torch.randn(4, 4, dtype=torch.float)
            b = torch.randn(4, 4, dtype=torch.float)
            nan = torch.tensor(float('nan'), dtype=torch.float)

            for f, inputs, device in product(
                    (tmax, tmin),
                    ([a, b], [a, nan], [b, nan]),
                    self.devices):
                inputs = [t.to(device) for t in inputs]
                s = self.checkScript(f, inputs)
                self.assertAllFused(s.graph_for(*inputs))

    def test_clamp(self):
        for device in self.devices:
            def func2(a, b):
                return torch.clamp(a + b, min=0, max=2)

            def funcInf(a, b):
                return torch.clamp(a + b, min=0, max=float('inf'))

            def funcNegInf(a, b):
                return torch.clamp(a + b, min=float('-inf'), max=0)

            def funcOptMin(a, b):
                return torch.clamp(a + b, max=2)

            def funcOptMax(a, b):
                return torch.clamp(a + b, min=0)

            a = torch.randn(4, 4, dtype=torch.float, device=device, requires_grad=True)
            b = torch.randn(4, 4, dtype=torch.float, device=device)
            nan = torch.tensor(float('nan'), dtype=torch.float, device=device)

            funcs = (func2, funcInf, funcNegInf, funcOptMin, funcOptMax)
            for f, inputs in product(funcs, [[a, b], [a, nan]]):
                inp1, inp2 = inputs
                s = self.checkScript(f, (inp1, inp2), profiling=ProfilingMode.PROFILING)
                self.assertAllFused(s.graph_for(inp1, inp2), except_for={'aten::size', 'aten::_size_if_not_equal'})
                c = s(inp1, inp2)
                with enable_profiling_mode_for_profiling_tests():
                    warmup_backward(c.sum())
                graph = backward_graph(s)
                self.assertAllFused(graph, except_for={'aten::Float', 'aten::_grad_sum_to_size'})

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skipIf(GRAPH_EXECUTOR != ProfilingMode.LEGACY, "no half support with profiling on")
    def test_dropout(self):
        def func(x):
            x = torch.nn.functional.dropout(x)
            return torch.nn.functional.relu(x)

        a = torch.randn(4, 4, dtype=torch.float, device='cuda', requires_grad=True)
        s = torch.jit.script(func)
        c = s(a)
        c = s(a)
        warmup_backward(c.sum())
        # skip_check to skip extra bailout nodes in between
        graph = backward_graph(s, skip_check=True)
        self.assertAllFused(graph, except_for={'aten::div', 'prim::Constant'})

    def test_add_bool(self):
        sizes = [(1,), (2,), (4, 4)]
        for device, size in product(self.devices, sizes):
            def f(x, y, z):
                return x + y + z

            x = torch.randint(0, 2, size, dtype=torch.bool, device=device)
            y = torch.randint(0, 2, size, dtype=torch.bool, device=device)
            z = torch.randint(0, 2, size, dtype=torch.bool, device=device)
            ge = self.checkTrace(f, (x, y, z), inputs_require_grads=False)
            self.assertAllFused(ge.graph_for(x, y, z))

    def test_mul_bool(self):
        for device in self.devices:
            def f(x, y, z):
                return x * y * z

            x = torch.randint(0, 2, (4, 4), dtype=torch.bool, device=device)
            y = torch.randint(0, 2, (4, 4), dtype=torch.bool, device=device)
            z = torch.randint(0, 2, (4, 4), dtype=torch.bool, device=device)

            ge = self.checkTrace(f, (x, y, z), inputs_require_grads=False)
            self.assertAllFused(ge.graph_for(x, y, z))

    def test_div_bool(self):
        for device in self.devices:
            def f(x, y, z):
                return (x + y) / z

            x = torch.randint(0, 2, (4, 4), dtype=torch.bool, device=device)
            y = torch.randint(0, 2, (4, 4), dtype=torch.bool, device=device)
            z = torch.ones_like(x, dtype=torch.bool, device=device)

            ge = self.checkTrace(f, (x, y, z), inputs_require_grads=False)
            self.assertAllFused(ge.graph_for(x, y, z))

    @unittest.skipIf(not LLVM_ENABLED, "TODO: bugs in ir eval")
    def test_bitwise_ops(self):
        def apply(fn):
            return lambda x, y, z: fn(fn(x, y), z)

        dtypes = [
            torch.int8,
            torch.uint8,
            torch.int16,
            torch.int32,
            torch.int64,
            torch.bool,
        ]
        binary_ops = [
            operator.__and__,
            operator.__or__,
            operator.__xor__
        ]
        devices = self.devices
        for dtype, op, device in product(dtypes, binary_ops, devices):
            try:
                x = self.data_for(dtype, device)
                y = self.data_for(dtype, device)
                z = self.data_for(dtype, device)
                fn = apply(op)
                ref = fn(x, y, z)
            except Exception:
                # If eager mode doesn't support a dtype/op/device combo,
                # neither does the fuser.  Catch everything to avoid needing to
                # guess what errors might be thrown by eager.
                continue
            try:
                t = torch.jit.trace(fn, (x, y, z))
                self.assertEqual(ref, t(x, y, z))
                self.assertAllFused(t.graph_for(x, y, z))
            except Exception as e:
                raise RuntimeError(
                    " ".join(["Failed:", str(dtype), op.__name__, device])
                )

    def test_minmax_int_ops(self):
        def apply(fn):
            return lambda x, y, z: fn(fn(x, y), z)

        dtypes = [
            torch.int8,
            torch.uint8,
            torch.int16,
            torch.int32,
            torch.int64,
            torch.bool,
        ]
        binary_ops = [
            torch.min,
            torch.max
        ]
        devices = self.devices
        for dtype, op, device in product(dtypes, binary_ops, devices):
            try:
                x = self.data_for(dtype, device)
                y = self.data_for(dtype, device)
                z = self.data_for(dtype, device)
                fn = apply(op)
                ref = fn(x, y, z)
            except Exception:
                # If eager mode doesn't support a dtype/op/device combo,
                # neither does the fuser.  Catch everything to avoid needing to
                # guess what errors might be thrown by eager.
                continue
            try:
                t = torch.jit.trace(fn, (x, y, z))
                self.assertEqual(ref, t(x, y, z))
                self.assertAllFused(t.graph_for(x, y, z))
            except Exception as e:
                raise RuntimeError(
                    " ".join(["Failed:", str(dtype), op.__name__, device])
                )

    def test_comparison_eq_ne(self):
        for device in self.devices:
            def f(x, y):
                mask = (x == 0).type_as(x)
                z = x * mask + y
                mask = (x != 0).type_as(x)
                z = z * mask + y
                return z

            x = torch.randn(4, 4, dtype=torch.float, device=device)
            y = torch.randn(4, 4, dtype=torch.float, device=device)

            ge = self.checkTrace(f, (x, y))
            self.assertAllFused(ge.graph_for(x, y))

    @staticmethod
    def fn_test_comparison_gt_lt(x, y):
        mask = (x > 0).type_as(x)
        z = x * mask + y
        mask = (x < 0).type_as(x)
        z = z * mask + y
        return z

    def test_comparison_gt_lt(self):
        for device in self.devices:
            x = torch.randn(4, 4, dtype=torch.float, device=device)
            y = torch.randn(4, 4, dtype=torch.float, device=device)

            ge = self.checkTrace(self.fn_test_comparison_gt_lt, (x, y))
            self.assertAllFused(ge.graph_for(x, y))

    def test_comparison_ge_le(self):
        for device in self.devices:
            def f(x, y):
                mask = (x >= 0).type_as(x)
                z = x * mask + y
                mask = (x <= 0).type_as(x)
                z = z * mask + y
                return z

            x = torch.randn(4, 4, dtype=torch.float, device=device)
            y = torch.randn(4, 4, dtype=torch.float, device=device)

            ge = self.checkTrace(f, (x, y))
            self.assertAllFused(ge.graph_for(x, y))
            x.requires_grad_(True)
            y.requires_grad_(True)
            self.assertAllFused(ge.graph_for(x, y), except_for=("aten::size", "prim::BroadcastSizes",
                                                                "aten::_size_if_not_equal"))

    def test_addcmul(self):
        for device in self.devices:
            t = torch.randn(1, 4, dtype=torch.float, device=device)
            t1 = torch.randn(4, 1, dtype=torch.float, device=device)
            t2 = torch.randn(1, 4, dtype=torch.float, device=device)

            def foo(t, t1, t2):
                return t.addcmul(t + 1, t2, value=0.1)

            ge = self.checkTrace(foo, (t, t1, t2), allow_unused=True)
            graph = ge.graph_for(t, t1, t2)
            fusion_groups = self.findFusionGroups(graph)
            self.assertEqual(len(fusion_groups), 1)
            FileCheck().check("aten::add(").check("aten::addcmul(").run(str(fusion_groups[0]))

    # TODO: We leak CUDA memory here because the traced graph holds onto a
    # constant-ified tensor. Since the Python-global CompilationUnit is alive
    # until the end of the process, the memory is effectively leaked.
    # Removed `_cuda` suffix from this test which disables leak-checking.
    # If this is a real problem, we'll need to revisit Torchscript Function
    # lifetimes in Python.
    def test_lerp(self):
        for device in self.devices:
            start = torch.randn(4, 1, dtype=torch.float, device=device)
            end = torch.randn(1, 4, dtype=torch.float, device=device)
            weight = torch.tensor(0.5, dtype=torch.float, device=device)

            # scalar weight overload
            def foo_weight_scalar(start, end):
                return torch.lerp(start + 1, end, 0.5)

            # tensor weight overload
            def foo_weight_tensor(start, end):
                return torch.lerp(start + 1, end, weight)

            ge_weight_scalar = self.checkTrace(foo_weight_scalar, (start, end))
            graph = ge_weight_scalar.graph_for(start, end)
            self.assertAllFused(graph)

            # TODO: uncomment when TE enables support for scalar tensors
            # ge_weight_tensor = self.checkTrace(foo_weight_tensor, (start, end))
            # graph = ge_weight_tensor.graph_for(start, end)
            # self.assertAllFused(graph)

    def test_concat(self):
        # disabling concat causes error with single concat node
        with set_fusion_group_inlining(True):
            for device in self.devices:
                hx = torch.randn(3, 20, dtype=torch.float, device=device)
                cx = torch.randn(3, 20, dtype=torch.float, device=device)

                def foo(hx, cx):
                    return torch.cat((hx + cx, hx * cx))

                ge = self.checkTrace(foo, (hx, cx))
                graph = ge.graph_for(hx, cx)
                self.assertAllFused(graph)
                # XXX: TE fuser can handle concats in a fusion group.
                # FileCheck().check("FusedConcat").check_next("return").run(str(graph))

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_remove_output_used_only_in_size(self):
        def test_fuse(a, b):
            c = a + b
            d = c + b
            return d

        scripted_f = torch.jit.script(test_fuse)
        x = torch.ones(1, requires_grad=True, device='cuda')
        y = torch.ones(1, requires_grad=True, device='cuda')
        warmup_forward(scripted_f, x, y)
        g = torch.jit.last_executed_optimized_graph()
        diff_nodes = [n for n in g.nodes() if n.kind() == 'prim::DifferentiableGraph']
        self.assertEqual(len(diff_nodes), 1)
        g = diff_nodes[0].g('Subgraph')
        if_nodes = [n for n in g.nodes() if n.kind() == 'prim::If']
        self.assertEqual(len(if_nodes), 1)
        # the if node and the fusion group inside it should only have one output
        self.assertEqual(len(list(if_nodes[0].outputs())), 1)

    def test_concat_invariant(self):
        for device in self.devices:
            # Invariant: the output of prim::FusedConcat may
            # not be an input to any node inside the FusionGroup.
            def fn(x, y, z):
                x1 = x + y
                y1 = x - y
                w = torch.cat([x1, y1])
                return w + z

            x = torch.randn(2, 2, dtype=torch.float, device=device)
            y = torch.randn(2, 2, dtype=torch.float, device=device)
            z = torch.randn(4, 2, dtype=torch.float, device=device)
            ge = self.checkTrace(fn, (x, y, z))
            graph = ge.graph_for(x, y, z)
            self.assertAllFused(graph, except_for={'aten::add'})
            # XXX: TE fuser can handle concats inside a fusion group.
            # FileCheck().check("FusedConcat").check_next("return").run(str(graph))

    @staticmethod
    def fn_test_exp(x, y):
        return (x + .5 * y).exp()

    def test_exp(self):
        for device in self.devices:
            x = torch.randn(4, 4, dtype=torch.float, device=device)
            y = torch.randn(4, 4, dtype=torch.float, device=device)

            ge = self.checkTrace(self.fn_test_exp, (x, y))
            self.assertAllFused(ge.graph_for(x, y))

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skipIf(GRAPH_EXECUTOR != ProfilingMode.LEGACY, "broken with profiling on")
    @torch._jit_internal._disable_emit_hooks_decorator
    @_inline_everything
    def test_fuse_decompose_normalization(self):
        class ResLike(torch.jit.ScriptModule):
            def __init__(self, norm_module):
                super(ResLike, self).__init__()
                self.nm = norm_module

            @torch.jit.script_method
            def forward(self, x, y):
                return y + torch.relu(self.nm(x))

        def test_norm_decompose(nm, in_opt_graph, not_in_opt_graph, in_fusegraph):
            model = ResLike(nm).cuda()
            model_noopt = ResLike(nm).cuda()
            model_noopt.load_state_dict(model.state_dict())
            x = torch.randn(2, 16, 8, 8, device='cuda')
            y = torch.randn(2, 16, 8, 8, device='cuda')

            # FIXME: We need differentiation for CNNs for this optimization to trigger
            with torch.no_grad():
                out = model(x, y)
                graph = model.graph_for(x, y)
                rep = str(graph)

                with torch.jit.optimized_execution(False):
                    out_noopt = model_noopt(x, y)
                    rep_noopt = str(model_noopt.graph_for(x, y))
                self.assertEqual(out, out_noopt, prec=3e-5)

            # Check that normalization op has really been decomposed
            for node_in_graph in in_opt_graph:
                self.assertIn(node_in_graph, rep)

            for node_not_in_graph in not_in_opt_graph:
                self.assertNotIn(node_not_in_graph, rep)
                self.assertIn(node_not_in_graph, rep_noopt)

            fusion_groups = [node for node in graph.nodes() if node.kind() == FUSION_GROUP]
            self.assertEqual(len(fusion_groups), 1)
            fused_graph = str(fusion_groups[0].g('Subgraph'))
            for node_in_fusegraph in in_fusegraph:
                self.assertIn(node_in_fusegraph, fused_graph)

        # test for batchnorm decompose
        bm = nn.BatchNorm2d(16)
        test_norm_decompose(bm, ['aten::batch_norm_update_stats'],
                            ['aten::batch_norm('], ['aten::sqrt'])

        # test for layernorm decompose
        lm = nn.LayerNorm(8)
        test_norm_decompose(lm, ['aten::batch_norm_stats'],
                            ['aten::layer_norm('], ['aten::sub', 'aten::mul', 'aten::add'])

    def test_threshold(self):
        for device in self.devices:
            def f(x):
                return torch.threshold(x, 0, -10) + x + x + x

            x = torch.tensor([-1, -0.5, 0, 1, 2, 3], device=device)
            scripted = self.checkScript(f, (x,))
            self.assertAllFused(scripted.graph_for(x))

    def test_scalar_arg(self):
        for device in self.devices:
            def fn_test_scalar_arg(x, p):
                # type: (Tensor, float) -> Tensor
                return p * (x * x + x)

            x = torch.randn(4, 4, dtype=torch.float, device=device)
            p = 3
            scripted = self.checkScript(fn_test_scalar_arg, (x, p))
            self.assertAllFused(scripted.graph_for(x, p))

            x.requires_grad_(True)

            # use another function otherwise we will bailout
            # and won't be able to do fused checks
            def fn_test_scalar_arg_requires_grad(x, p):
                # type: (Tensor, float) -> Tensor
                return p * (x * x + x)

            scripted = torch.jit.script(fn_test_scalar_arg_requires_grad)
            out = scripted(x, p)
            self.assertAllFused(scripted.graph_for(x, p), except_for=("aten::size", "prim::BroadcastSizes",
                                                                      "aten::_size_if_not_equal"))

    @unittest.skip("deduplicating introduces aliasing in backward graph's outputs")
    def test_fuser_deduplication(self):
        # See that fusion kernel outputs are deduplicated when removing  _grad_sum_to_size in the fuser's compilation
        # see the discussion in PR #14957.
        def f(x, y):
            return torch.sigmoid(x + y)

        b = torch.randn(5, 5, requires_grad=True)
        a = torch.randn(5, 5, requires_grad=True)
        s = self.checkScript(f, (a, b))
        self.assertAllFused(s.graph_for(a, b), except_for={
                            'aten::size', 'aten::_size_if_not_equal', 'prim::BroadcastSizes'})

        c = s(a, b)
        results = warmup_backward(c.sum(), [a, b])
        ga2, gb2 = results.pop()
        graph = backward_graph(s)
        self.assertAllFused(graph)
        # check that a, b share storage, i.e. were generated as a single output in the fuser
        self.assertEqual(ga2.data_ptr(), gb2.data_ptr())

    @unittest.skip("temporarily disabled because fusion was restricted in fixing #22833")
    def test_fuser_iou(self):
        # This checks if most of Intersection over Union is fused.
        # In particular, the backward contains many _grad_sum_to_size.
        def iou(b1x1, b1y1, b1x2, b1y2, b2x1, b2y1, b2x2, b2y2):
            ltx = torch.max(b1x1, b2x1)  # [N,M]
            lty = torch.max(b1y1, b2y1)
            rbx = torch.min(b1x2, b2x2)
            rby = torch.min(b1y2, b2y2)

            w = (rbx - ltx).clamp(min=0, max=float('inf'))  # [N,M]
            h = (rby - lty).clamp(min=0, max=float('inf'))  # [N,M]
            inter = w * h  # [N,M]

            area1 = (b1x2 - b1x1) * (b1y2 - b1y2)  # [N,1]
            area2 = (b2x2 - b2x1) * (b2y2 - b2y2)  # [1,M]
            iou = inter / (area1 + area2 - inter)
            return iou

        box1 = torch.randn(5, 4, requires_grad=True)
        box2 = torch.randn(5, 4, requires_grad=True)
        # unsqueezing can currently not be fused
        b1x1 = box1[:, 0].unsqueeze(1)  # [N,1]
        b1y1 = box1[:, 1].unsqueeze(1)
        b1x2 = box1[:, 2].unsqueeze(1)
        b1y2 = box1[:, 3].unsqueeze(1)
        b2x1 = box2[:, 0].unsqueeze(0)  # [1,N]
        b2y1 = box2[:, 1].unsqueeze(0)
        b2x2 = box2[:, 2].unsqueeze(0)
        b2y2 = box2[:, 3].unsqueeze(0)

        s = self.checkScript(iou, (b1x1, b1y1, b1x2, b1y2, b2x1, b2y1, b2x2, b2y2))
        self.assertAllFused(s.graph_for(b1x1, b1y1, b1x2, b1y2, b2x1, b2y1, b2x2, b2y2),
                            except_for={'aten::size', 'prim::BroadcastSizes', 'aten::_size_if_not_equal'})

        with enable_profiling_mode_for_profiling_tests(True):
            c = s(b1x1, b1y1, b1x2, b1y2, b2x1, b2y1, b2x2, b2y2)
            warmup_backward(c.sum(), [b1x1, b1y1, b1x2, b1y2, b2x1, b2y1, b2x2, b2y2])
            graph = backward_graph(s)
            self.assertAllFused(graph, except_for={'aten::size', 'prim::BroadcastSizes', 'aten::_size_if_not_equal'})

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skipIf(not RUN_CUDA_MULTI_GPU, "needs non-zero device")
    def test_fusion_reuse_multi_gpu(self):
        def fn(x, y):
            return x * y * x * y

        inputs_cpu = [
            torch.randn(4, 4, dtype=torch.float),
            torch.randn(4, 4, dtype=torch.float),
        ]
        inputs_cuda0 = [x.cuda(0) for x in inputs_cpu]
        inputs_cuda1 = [y.cuda(1) for y in inputs_cpu]

        # Should not crash; these should compile different kernels.
        ge = self.checkScript(fn, inputs_cpu)
        self.assertAllFused(ge.graph_for(*inputs_cpu))
        ge(*inputs_cuda0)
        ge(*inputs_cuda1)

    # TODO: we're currently not checking 'device' in the type info when pulling
    # nodes into a fusion group. We should fix that and re-enable this test.
    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skipIf(not RUN_CUDA_MULTI_GPU, "needs non-zero device")
    def test_kernel_cache_multi_gpu(self):
        def not_fusible(x):
            return x

        def fn(x, y, z):
            x_out = x * x * x * x * x  # fusion: lambda x. x * x * x * x * x
            y_out = y * y * y * y * y
            z_out = z * z * z * z * z
            return not_fusible(x_out), not_fusible(y_out), not_fusible(z_out)

        inputs = [
            torch.randn(4, 4, dtype=torch.float),
            torch.randn(4, 4, dtype=torch.float, device='cuda:0'),
            torch.randn(4, 4, dtype=torch.float, device='cuda:1'),
        ]

        prev_cache_size = torch._C._jit_debug_fuser_num_cached_kernel_specs()

        # There are 3 FusionGroups. Because they have the same graph, they
        # should reuse the same KernelSpec in the KernelSpec cache.
        ge = self.checkScript(fn, inputs)
        self.assertGraphContainsExactly(
            ge.graph_for(*inputs), FUSION_GROUP, 3, True)
        new_cache_size = torch._C._jit_debug_fuser_num_cached_kernel_specs()
        # XXX: This assumes that the same kernel isn't already used by another test
        # FIXME: Use the TE fuser's way of querying the cache.
        # self.assertEqual(new_cache_size - prev_cache_size, 1)

    @unittest.skipIf(not RUN_CUDA_MULTI_GPU, "needs non-zero device")
    def test_nonzero_device_cuda(self):
        device = 'cuda:' + str(1)
        x = torch.tensor([0.4], dtype=torch.float, device=device)
        y = torch.tensor([0.7], dtype=torch.float, device=device)

        def doit(x, y):
            return torch.sigmoid(torch.tanh(x * (x + y) + x))

        ge = self.checkTrace(doit, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    def test_lstm(self):
        for device in self.devices:
            inputs = get_lstm_inputs(device, training=True)
            module = self.checkScript(LSTMCellS, inputs)
            self.assertLastGraphAllFused()

    def test_lstm_concat(self):
        # single fusion node causes error
        with set_fusion_group_inlining(True):
            for device in self.devices:
                inputs = get_lstm_inputs(device)
                ge = self.checkTrace(LSTMCellC, inputs)
                graph = ge.graph_for(*inputs)
                self.assertLastGraphAllFused()
                # XXX: TE fuser can handle concats inside a fusion group.
                # FileCheck().check("FusedConcat").check_next("return").run(str(graph))

    def test_lstm_gates_permutations(self):
        for device in self.devices:
            # lstm has gates = x.mm(w_ih.t()) + hx.mm(w_hh.t()) + b_ih + b_hh.
            # Test that any permutation of this will still result in one FusionGroup.
            choices = ['x.mm(w_ih.t())', 'hx.mm(w_hh.t())', 'b_ih', 'b_hh']
            template = dedent('''
            def cell(x, hx, cx, w_ih, w_hh, b_ih, b_hh):
                gates = {} + {} + {} + {}
                ingate, forgetgate, cellgate, outgate = gates.chunk(4, 1)
                return ingate * forgetgate * cellgate * outgate
            ''')
            for permutation in permutations(choices, len(choices)):
                code = template.format(*permutation)
                scope = {}
                exec(code, globals(), scope)
                cu = torch.jit.CompilationUnit(code)

                inputs = get_lstm_inputs(device, training=False)
                self.assertEqual(cu.cell(*inputs), scope['cell'](*inputs))
                forward_graph = cu.cell.graph_for(*inputs)
                self.assertGraphContainsExactly(forward_graph, FUSION_GROUP, 1)

    # TODO: Fuser doesn't work at all when inputs require grad. Fix that
    def test_lstm_traced(self):
        for device in self.devices:
            inputs = get_lstm_inputs(device)
            ge = self.checkTrace(LSTMCellF, inputs)
            graph = ge.graph_for(*inputs)
            fusion_groups = self.findFusionGroups(graph)
            self.assertEqual(len(fusion_groups), 1)
            FileCheck().check("Chunk").check("aten::sigmoid").check("aten::tanh").run(str(fusion_groups[0]))

    def test_milstm(self):
        for device in self.devices:
            inputs = get_milstm_inputs(device, training=True)
            module = self.checkScript(MiLSTMCell, inputs)
            forward_graph = module.graph_for(*inputs)
            self.assertGraphContainsExactly(
                forward_graph, FUSION_GROUP, 1, consider_subgraphs=True)
            FileCheck().check("DifferentiableGraph").check_next("TupleConstruct") \
                .check_next("return").check(FUSION_GROUP).run(str(forward_graph))
            hy, cy = module(*inputs)
            warmup_backward((hy + cy).sum())

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skip("rand_like is not supported yet")
    def test_rand_cuda(self):
        class M(torch.jit.ScriptModule):
            __constants__ = ['d']

            def __init__(self):
                super(M, self).__init__()
                self.d = torch.device('cuda')

            @torch.jit.script_method
            def create(self, x):
                return x * x + x + torch.rand_like(x)

        x = torch.zeros([3, 4, 5], dtype=torch.float, device='cuda')
        m = M()
        out1 = m.create(x)
        cx = CudaCodeGenExecuted()
        out2 = m.create(x)
        assert cx.elapsed_value() == 1
        self.assertNotEqual(out1, out2)
        self.assertTrue(torch.all(out1 >= 0))
        self.assertTrue(torch.all(out1 < 1))
        self.assertTrue(torch.all(out2 >= 0))
        self.assertTrue(torch.all(out2 < 1))
        self.assertAllFused(m.create.graph_for(x))

    @staticmethod
    def fn_test_relu(x, y):
        return F.relu(x + .5 * y)

    def test_relu(self):
        for device in self.devices:
            x = torch.randn(4, 4, dtype=torch.float, device=device)
            y = torch.randn(4, 4, dtype=torch.float, device=device)

            ge = self.checkTrace(self.fn_test_relu, (x, y))
            self.assertAllFused(ge.graph_for(x, y))

    def test_erf(self):
        for device in self.devices:
            def fn_test_erf(x):
                return F.relu(torch.erf(x) - torch.erfc(x))

            x = torch.randn(4, 4, dtype=torch.float, device=device)
            ge = self.checkTrace(fn_test_erf, (x,))
            self.assertAllFused(ge.graph_for(x))
            x.requires_grad_(True)
            ge = self.checkTrace(fn_test_erf, (x,))
            self.assertAllFused(ge.graph_for(x), except_for=("aten::size", "prim::BroadcastSizes",
                                                             "aten::_size_if_not_equal"))

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skip("rand_like is not supported yet")
    def test_rand_broadcast_cuda(self):
        def fn_test_rand(x, y):
            r = torch.rand_like(y)
            return r * x + x

        # If using profiling, a different function is needed to test different
        # shapes, or we'll use a cached script.
        def fn_test_rand2(x, y):
            r = torch.rand_like(y)
            return r * x * x

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')
        script_f = torch.jit.script(fn_test_rand)
        warmup_forward(script_f, x, y)
        out = script_f(x, y)
        self.assertAllFused(script_f.graph_for(x, y))
        x.requires_grad_(True)
        out = script_f(x, y)
        self.assertAllFused(script_f.graph_for(x, y), except_for=("aten::size", "prim::BroadcastSizes",
                                                                  "aten::_size_if_not_equal"))

        # test that broadcasting random produces correct results
        x = torch.ones(4, 4, dtype=torch.float, device='cuda')
        y = torch.ones(4, dtype=torch.float, device='cuda')
        script_f = torch.jit.script(fn_test_rand2)
        warmup_forward(script_f, x, y)
        out = script_f(x, y)
        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(out[0, :] + torch.zeros(4, 4, device='cuda'), out)

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skip("rand_like is not supported yet")
    def test_rand_diamond(self):
        def fn_test_diamond(x, y):
            r = torch.rand_like(y)
            a = x + r
            b = y - r
            return a + b

        x = torch.randn(4, 4, dtype=torch.float, device='cuda')
        y = torch.randn(4, 4, dtype=torch.float, device='cuda')
        script_f = torch.jit.script(fn_test_diamond)
        warmup_forward(script_f, x, y)
        cx = CudaCodeGenExecuted()
        out = script_f(x, y)
        assert cx.elapsed_value() == 1
        self.assertEqual(out, x + y)

    @unittest.skip("Reenable when TE will add support for 0-dim tensors")
    def test_scalar(self):
        def fn(x, y):
            return 2 * x + y

        x = torch.tensor(0.1, dtype=torch.float, device='cpu')
        y = torch.tensor(1, dtype=torch.float, device='cpu')
        ge = self.checkScript(fn, (x, y))
        self.assertAllFused(ge.graph_for(x, y))

    def test_small_constant(self):
        for device in self.devices:
            def fn_test_small_constant(x, y):
                return (1e-8 * x + 5e-9 * y) * 1e8
            x = torch.randn(4, 4, dtype=torch.float, device=device)
            y = torch.randn(4, 4, dtype=torch.float, device=device)

            ge = self.checkTrace(fn_test_small_constant, (x, y))
            self.assertAllFused(ge.graph_for(x, y))

    # Currently we don't pull constants into fusion groups, because in some
    # cases it could remove the constant from the original graph and now our
    # fusion group needs to return that constant for its other users.
    # Instead of never pulling constants into the fusion group, we should just
    # be more careful at how we rewrite its users.
    # TODO: fix that and reenable the test.
    def test_tensor_scalar_ops(self):
        for device in self.devices:
            def should_fuse(x):
                z = 3.
                y = x + z
                return x * y

            def should_fuse_scalar(x, z):
                y = x + int(z)
                return x * y

            inputs = [torch.randn(2, 2, dtype=torch.float, device=device)]
            ge = self.checkScript(should_fuse, inputs)
            graph = ge.graph_for(*inputs)
            fusion_groups = self.findFusionGroups(graph)
            self.assertEqual(len(fusion_groups), 1)
            FileCheck().check("aten::add").check("aten::mul").run(str(fusion_groups[0]))

            inputs = [
                torch.randn(2, 2, dtype=torch.float, device=device),
                torch.tensor(3., dtype=torch.float, device=device),
            ]
            ge = self.checkScript(should_fuse_scalar, inputs)
            # Check that the fused graph computes correct results when the scalar
            # input changes.
            inputs = [
                torch.randn(2, 2, dtype=torch.float, device=device),
                torch.tensor(7., dtype=torch.float, device=device),
            ]
            self.assertEqual(ge(*inputs), should_fuse_scalar(*inputs))
            # The TE fuser supports fusion of non-constant scalars
            self.assertGraphContainsExactly(
                ge.graph_for(*inputs), FUSION_GROUP, 1, consider_subgraphs=True)

    def test_where_and_typing(self):
        for device in self.devices:
            def f(x, y):
                mask = x > y
                res = torch.where(mask, x, y)
                return mask, res

            x = torch.randn(4, 4, dtype=torch.double, device=device)
            y = torch.randn(4, 4, dtype=torch.double, device=device)

            script_f = self.checkScript(f, (x, y))
            self.assertAllFused(script_f.graph_for(x, y), except_for={'prim::TupleConstruct'})

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    @unittest.skipIf(GRAPH_EXECUTOR != ProfilingMode.LEGACY, "no half support with profiling on")
    def test_grad_sum_to_size_elimination(self):

        def my_broadcasted_cell(a, b, c):
            return (a + b) + c

        s1 = torch.randn(5, 1, requires_grad=True, device='cuda')
        s2 = torch.randn(5, 5, requires_grad=True, device='cuda')

        module = self.checkScript(my_broadcasted_cell, (s1, s1, s1), profiling=ProfilingMode.PROFILING)
        forward_graph = module.graph_for(s1, s1, s1)
        self.assertAllFused(forward_graph, except_for=("aten::size", "prim::BroadcastSizes",
                                                       "aten::_size_if_not_equal"))

        old_plans = set()
        for i in range(3):
            # if we have s2, then the s1 are _grad_sum_to_size'd

            args = s2 if i < 1 else s1, s2 if i < 2 else s1, s2
            args = [a.detach_().requires_grad_() for a in args]
            # recompile, so we don't trigger bailouts
            module = self.checkScript(my_broadcasted_cell, args, profiling=ProfilingMode.PROFILING)
            res = module(s2 if i < 1 else s1, s2 if i < 2 else s1, s2)
            warmup_backward(res.sum(), args)
            grads = torch.autograd.grad(res.sum(), args)
            for inp, gr in zip(args, grads):
                self.assertEqual(inp.shape, gr.shape)
            backward = None
            # this is a workaround for the backward graphs not being
            # in order for Python 2
            for g in all_backward_graphs(module):
                if str(g) not in old_plans:
                    assert backward is None
                    backward = g
                    old_plans.add(str(backward))
            num_grads = 1 if i > 0 else 0
            self.assertEqual(len([n for n in backward.nodes() if n.kind() == 'aten::_grad_sum_to_size']), num_grads)

    def test_disabled(self):
        old_cpu_fuser_state = torch._C._jit_can_fuse_on_cpu()
        torch._C._jit_override_can_fuse_on_cpu(False)

        def fn(a):
            return a ** 2 + a

        x = torch.randn(4, dtype=torch.float, device="cpu")
        s = self.checkScript(fn, (x,))
        g = s.graph_for(x)
        self.assertEqual(len(self.findFusionGroups(g)), 0)

        torch._C._jit_override_can_fuse_on_cpu(old_cpu_fuser_state)

    def data_for(self, dtype, device="cuda", size=None):
        if size is None:
            v = torch.arange(1, 3, dtype=torch.float, device=device)
        else:
            v = torch.rand(*size, device=device)
        if dtype == torch.bool:
            return v > 2
        elif dtype in [torch.qint8, torch.quint8, torch.qint32]:
            return torch.quantize_per_tensor(v, 0.1, 1, dtype=dtype)
        else:
            return v.to(dtype)

    @unittest.skipIf(not LLVM_ENABLED, "TODO: bugs in ir eval")
    def test_unary_ops(self):
        def apply(fn):
            return lambda x: fn(x)

        dtypes = [
            torch.int8,
            torch.uint8,
            torch.int16,
            torch.int32,
            torch.int64,
            torch.float16,
            torch.float32,
            torch.float64,
            torch.bool,
        ]
        unary_ops = [
            torch.sigmoid,
            torch.reciprocal,
            torch.neg,
            torch.relu,
            torch.log,
            torch.log10,
            torch.log2,
            torch.exp,
            torch.expm1,
            torch.erf,
            torch.erfc,
            torch.cos,
            torch.sin,
            torch.tan,
            torch.acos,
            torch.asin,
            torch.cosh,
            torch.sinh,
            torch.atan,
            torch.tanh,
            torch.sqrt,
            torch.rsqrt,
            torch.abs,
            torch.ceil,
            torch.floor,
            torch.round,
            torch.trunc,
            torch.frac,
            lambda x: torch.threshold(x, 0, -10),
            # FIXME: fails on cpu with dtype=uint8
            # lambda x: torch.clamp(x, -10, 10),
        ]
        sizes = [(1,), (2,), (4, 4)]
        for dtype, op, device, size in product(dtypes, unary_ops, self.devices, sizes):
            try:
                x = self.data_for(dtype, device, size=size)
                fn = apply(op)
                ref = fn(x)
            except Exception:
                # If eager mode doesn't support a dtype/op/device combo,
                # neither does the fuser.  Catch everything to avoid needing to
                # guess what errors might be thrown by eager.
                continue
            try:
                t = torch.jit.trace(fn, (x,))
                torch.testing.assert_allclose(ref, t(x))
                self.assertAllFused(t.graph_for(x))
            except Exception as e:
                raise RuntimeError(
                    " ".join(["Failed:", str(dtype), op.__name__, device, str(size)])
                )

    @unittest.skipIf(not LLVM_ENABLED, "TODO: bugs in ir eval")
    def test_binary_ops(self):
        def apply(fn):
            return lambda x, y: fn(x, y)

        dtypes = [
            torch.int8,
            torch.uint8,
            torch.int16,
            torch.int32,
            torch.int64,
            torch.float16,
            torch.float32,
            torch.float64,
            torch.bool,
        ]
        binary_ops = [
            operator.__and__,
            operator.__or__,
            operator.__xor__,
            torch.add,
            torch.sub,
            torch.mul,
            torch.min,
            torch.max,
            lambda x, y: torch.lerp(x, y, 0.5),
            # FIXME: comparison ops yield different results when fused
            # torch.eq,
            # torch.ne,
            # torch.ge,
            # torch.gt,
            # torch.lt,

            # FIXME: these ops produce different results with int dtype
            # torch.pow,
            # torch.atan2,

            # TODO: test operators exercising division too
            # torch.fmod,
            # torch.remainder,
            # operator.__rshift__,
            # operator.__lshift__,
            # torch.div,
        ]
        devices = self.devices
        for dtype, op, device in product(dtypes, binary_ops, devices):
            try:
                x = self.data_for(dtype, device)
                y = self.data_for(dtype, device)
                fn = apply(op)
                ref = fn(x, y)
            except Exception:
                # If eager mode doesn't support a dtype/op/device combo,
                # neither does the fuser.  Catch everything to avoid needing to
                # guess what errors might be thrown by eager.
                continue
            try:
                t = torch.jit.trace(fn, (x, y))
                self.assertEqual(ref, t(x, y))
                self.assertAllFused(t.graph_for(x, y))
            except Exception as e:
                raise RuntimeError(
                    " ".join(["Failed:", str(dtype), op.__name__, device])
                )

    @unittest.skipIf(not LLVM_ENABLED, "TODO: bugs in ir eval")
    def test_binary_ops_tensor_scalar(self):
        def apply_with_scalar(fn, scalar):
            return lambda x: fn(x, scalar)

        dtypes = [
            torch.int8,
            torch.uint8,
            torch.int16,
            torch.int32,
            torch.int64,
            torch.float16,
            torch.float32,
            torch.float64,
            # torch.bool intentionally not included
        ]
        binary_ops = [
            operator.__and__,
            operator.__or__,
            operator.__xor__,
            torch.add,
            torch.sub,
            torch.mul,
            torch.eq,
            torch.ne,

            # FIXME: fails with dtype=uint8, scalar=-1
            # torch.ge,
            # torch.lt,
            # torch.gt,

            # FIXME: fails with integer dtype and scalar={3,0}
            # torch.div,

            # FIXME: fails with dtype=torch.float16/device=cuda/scalar=0
            # torch.pow,

            # FIXME: segfaults on CPU backend
            # operator.__rshift__,
            # operator.__lshift__,

            # FIXME: wrong results with int8 on cpu
            # torch.remainder,
            # torch.fmod,
        ]
        devices = self.devices
        # Maybe we should split this into separate tests to speed it up by
        # only using  scalar values relevant to particular ops
        scalars = [1.5, 3, 0, -2.0, -1]
        for dtype, op, device, scalar in product(dtypes, binary_ops, devices, scalars):
            try:
                x = self.data_for(dtype, device)
                fn = apply_with_scalar(op, scalar)
                ref = fn(x)
            except Exception:
                # If eager mode doesn't support a dtype/op/device combo,
                # neither does the fuser.  Catch everything to avoid needing to
                # guess what errors might be thrown by eager.
                continue
            try:
                t = torch.jit.trace(fn, (x))
                self.assertEqual(ref, t(x))
                self.assertAllFused(t.graph_for(x))
            except Exception as e:
                raise RuntimeError(
                    " ".join(["Failed:", str(dtype), op.__name__, device])
                )

    @unittest.skipIf(not LLVM_ENABLED, "TODO: enable in ir eval")
    def test_ternary_ops(self):
        def apply(fn):
            return lambda x, y, z: fn(x, y, z)

        dtypes = [
            torch.int8,
            torch.uint8,
            torch.int16,
            torch.int32,
            torch.int64,
            torch.float16,
            torch.float32,
            torch.float64,
            torch.bool,
        ]
        ternary_ops = [
            torch.lerp,
            torch.addcmul,
        ]
        devices = self.devices
        for dtype, op, device in product(dtypes, ternary_ops, devices):
            try:
                x = self.data_for(dtype, device)
                y = self.data_for(dtype, device)
                z = self.data_for(dtype, device)
                fn = apply(op)
                ref = fn(x, y, z)
            except Exception:
                # If eager mode doesn't support a dtype/op/device combo,
                # neither does the fuser.  Catch everything to avoid needing to
                # guess what errors might be thrown by eager.
                continue
            try:
                t = torch.jit.trace(fn, (x, y, z))
                self.assertEqual(ref, t(x, y, z))
                self.assertAllFused(t.graph_for(x, y, z))
            except Exception as e:
                raise RuntimeError(
                    " ".join(["Failed:", str(dtype), op.__name__, device])
                )

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_unsupported_dtypes(self):
        def fn(x):
            return x * x + x

        unsupported_dtypes = [
            torch.bfloat16,
            torch.complex32,
            torch.complex64,
            torch.complex128,
            torch.qint8,
            torch.quint8,
            torch.qint32,
        ]
        for dtype in unsupported_dtypes:
            try:
                x = self.data_for(dtype, "cuda")
                ref = fn(x)
            except Exception:
                # If eager mode doesn't support a dtype/op/device combo,
                # neither does the fuser.  Catch everything to avoid needing to
                # guess what errors might be thrown by eager.
                continue
            t = torch.jit.trace(fn, (x,))
            self.assertEqual(ref, t(x))
            self.assertEqual(len(self.findFusionGroups(t.graph_for(x))), 0)

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_superslomo(self):
        # Test extracted from Super-SloMo: https://github.com/avinashpaliwal/Super-SloMo
        # A few interesting things happen here: strided inputs of mixed size,
        # plus outputs of mixed shapes.  The latter characteristic happened to
        # expose a memory corruption bug due to not properly guarding the
        # outputs.
        def eager(t0, t1, t2, t3, t4):
            t5 = torch.mul(t0, t4)
            t6 = torch.mul(t2, t3)
            t7 = torch.mul(t6, t1)
            t9 = torch.add(t5, t7)
            t11 = torch.add(t0, t6)
            ft_p = torch.div(t9, t11)
            return (ft_p, t11, t9, t6)

        t0 = torch.rand(1, 6, 352, 352, device="cuda").transpose(0, 1)
        t1 = torch.rand(6, 3, 352, 352, device="cuda")
        t2 = torch.rand(6, device="cuda")[None, None, None, :].permute(3, 0, 1, 2)
        t3 = torch.rand(6, 1, 352, 352, device="cuda")
        t4 = torch.rand(6, 3, 352, 352, device="cuda")
        inputs = [t0, t1, t2, t3, t4]

        script = torch.jit.script(eager)
        for _ in range(4):
            for pair in zip(script(*inputs), eager(*inputs)):
                test, ref = pair
                torch.testing.assert_allclose(test, ref)
        self.assertAllFused(script.graph_for(*inputs))

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_sub_gt_and(self):
        def eager(t1, t2, t3, t4, t: float):
            w = t1 - t2
            h = t3 - t4
            k = (w > t) & (h > t)
            assert k.dtype == torch.bool
            if t > 0.5:
                # Putting a use of k in a never-executed conditional prevents
                # profiling its type, which leaves it as "Tensor".  If we
                # propagate Tensor back to the definition of k, we have to be
                # careful not to create a fusion group containing it.
                return k + 1
            return w
        t = torch.rand(8, dtype=torch.float, device='cuda')
        scripted = self.checkScript(eager, (t, t, t, t, 0.1))

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_chunk_mul_one(self):
        def eager(x):
            z, y, w = torch.chunk(x, 3, -1)
            return z * 3, y, w
        x = torch.rand(64, 1, 3072, dtype=torch.float, device='cuda')
        script = self.checkScript(eager, (x,))

    @unittest.skipIf(not RUN_CUDA, "fuser requires CUDA")
    def test_eq_unsqueeze_type_as(self):
        def eager(a, b):
            mask = b == 1
            mask = torch.unsqueeze(mask, -1)
            x = mask.type_as(a)
            return x, mask
        a = torch.rand(1, 64, 1024, device='cuda', dtype=torch.float)
        b = torch.randint(-2, 2, (1, 64), device='cuda', dtype=torch.long)
        script = self.checkScript(eager, (a, b))

    def test_neg_pow(self):
        def eager_tt(a: torch.Tensor, b: torch.Tensor):
            return torch.neg(torch.pow(a, b))

        def eager_ts(a: torch.Tensor, b: float):
            return torch.neg(torch.pow(a, b))

        def eager_st(a: float, b: torch.Tensor):
            return torch.neg(torch.pow(a, b))

        a = torch.rand(1, dtype=torch.float)
        b = torch.rand(1, dtype=torch.float)
        s = b.item()
        script = self.checkScript(eager_tt, (a, b))
        self.assertAllFused(script.graph_for(a, b))
        script = self.checkScript(eager_ts, (a, s))
        self.assertAllFused(script.graph_for(a, s))
        script = self.checkScript(eager_st, (s, b))
        self.assertAllFused(script.graph_for(s, b))

if __name__ == '__main__':
    run_tests()
