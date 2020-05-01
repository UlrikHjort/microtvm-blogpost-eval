# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""
Auto-tuning convolution on ARM Cortex-M7 STM32F746 Boards
=====================================================================
**Author**: `Logan Weber <https://github.com/weberlo>`_

TODO More docs
"""
import json
import logging
import os
import sys
from collections import OrderedDict

from mxnet.gluon.model_zoo import vision
import numpy as np
from PIL import Image

import topi
import tvm
from tvm import rpc, autotvm, relay
from tvm.contrib import graph_runtime, util, download

from tvm.autotvm.tuner import XGBTuner, GATuner, RandomTuner, GridSearchTuner

import tvm.micro as micro
from tvm.micro import create_micro_mod
from tvm.micro.device import host
from tvm.micro.device.arm import stm32f746xx
from tvm.micro.device.arm.stm32f746xx import MemConstraint

from tvm.relay import transform
from tvm.relay.op import nn

from topi.util import get_const_tuple
from topi.nn.util import get_const_int, get_pad_tuple
from topi.nn.conv2d import conv2d, conv2d_nchw
from topi.generic import schedule_conv2d_nchw
from topi.nn.pad import pad
from topi.nn.util import get_pad_tuple
from topi.util import simplify, get_const_tuple, traverse_inline

from micro_eval.util import (
    CMSIS_PATH, CMSIS_INCLUDE_PATHS,
    NamedTensor, NamedType, BakedType,
    print_c_source,
    custom_pick_best,
    relay_micro_build, reset_gdbinit,
    get_comm_overhead, benchmark_micro_func,
    check_conv2d_output
)
from micro_eval.model.cifar10_cnn import gen_cifar10_cnn
from micro_eval.micro_topi import collect_conv_tasks
from micro_eval.micro_topi.cortex_m7.conv2d.direct import conv2d_direct
from micro_eval.micro_topi.cortex_m7.conv2d.direct_simd import conv2d_direct_simd
from micro_eval.micro_topi.cortex_m7.conv2d.partial_im2col import conv2d_partial_im2col

################
# Instructions #
################
#
# First, locate your OpenOCD script directory (e.g.,
# OPENOCD_SCRIPT_DIR=/usr/share/openocd/scripts) and run
#   `openocd -f $(OPENOCD_SCRIPT_DIR)/interface/stlink-v2-1.cfg -f $(OPENOCD_SCRIPT_DIR)/target/stm32f7x.cfg`
# in one terminal.
#
# If you want to connect multiple boards, you will need to
# identify the serial number of the JTAG adapter for each board.  To do so, use
# this trick:
# https://stackoverflow.com/questions/29121050/openocd-debugging-multiple-devices-at-once
#
# Once you have the serial numbers, create an OpenOCD `.cfg` file for each one,
# using the following template:
#   source [find target/stm32f7x.cfg]
#   hla_serial $SERIAL_NUMBER
#   gdb_port $GDB_PORT
#   tcl_port $TCL_PORT
#   telnet_port $TELNET_PORT
# Make sure that in each config file, the GDB, Tcl, and Telnet ports are unique
# across all config files.  We only care about the Tcl port, but OpenOCD will
# quit if *any* of the ports are already in use.
#
# With the config files created, use the following command, replacing
# $BOARD_CONFIG_FILE with each board's respective config file:
#   `openocd -f $(OPENOCD_SCRIPT_DIR)/interface/stlink-v2-1.cfg -f $BOARD_CONFIG_FILE
#
# Then, run
#   `python -m tvm.exec.rpc_tracker --host 0.0.0.0 --port=9190`
# in another terminal.
#
# Then, run
#   `python -m tvm.exec.rpc_server --tracker=0.0.0.0:9190 --key=arm.stm32f746xx --utvm-dev-id='arm.stm32f746xx' --utvm-dev-config-args='["127.0.0.1", 6666]'`
# in another terminal.  If you have multiple boards, you will need to run this
# command for each board, adjusting the port accordingly.
#
# To make sure your device(s) are connected to the tracker, run
#   `python -m tvm.exec.query_rpc_tracker --port 9190`
#

####################
# Autotuning Setup #
####################
logging.getLogger('autotvm').setLevel(logging.DEBUG)
logging.getLogger('autotvm').addHandler(logging.StreamHandler(sys.stdout))

DEVICE_ID = host.DEVICE_ID
TARGET = tvm.target.create('c -device=micro_dev')

if DEVICE_ID == stm32f746xx.DEVICE_ID:
    SERVER_ADDR = '127.0.0.1'
    SERVER_PORT = 6666
    def generate_config(section_constraints=None):
        return stm32f746xx.generate_config(
            SERVER_ADDR,
            SERVER_PORT,
            section_constraints=section_constraints)
    MICRO_HEADERS = CMSIS_HEADERS
    MICRO_INCLUDE_PATHS = CMSIS_INCLUDE_PATHS
    # per-conv op strategies (first entry is the strategy of the first conv and so on).
    # we want the ability to configure the op strategy, instead of just using
    # the best strategy in the log, because certain strategy combos have a
    # memory footprint that exceeds the available memory of the device.
    OP_STRATEGIES = [
        conv2d_direct,
        conv2d_direct_simd,
        conv2d_direct_simd,
        ]
elif DEVICE_ID == host.DEVICE_ID:
    def generate_config(section_constraints=None):
        return host.generate_config(section_constraints=section_constraints)
    MICRO_HEADERS = None
    MICRO_INCLUDE_PATHS = None
    # we don't have SIMD schedules for the host
    OP_STRATEGIES = [
        conv2d_direct,
        conv2d_direct,
        conv2d_direct,
        ]
else:
    raise RuntimeErorr(f'unknown device ID "{DEVICE_ID}"')

DEV_CONFIG = generate_config()

N_TRIAL = 500
EARLY_STOPPING = 250
# N_TRIAL = 1
# EARLY_STOPPING = 1

N_PER_TRIAL = 15
N_PARALLEL = None

TRACKER_ADDR = '0.0.0.0'
TRACKER_PORT = 9190

TUNE_OPS = [relay.op.nn.conv2d]

# disable timeouts because JTAG is slow
TIMEOUT = 0

#############
# Debugging #
#############
# NOTE in the autotvm setting, this is only useful if there's only one RPC server running
# reset_gdbinit(DEV_CONFIG)

###################
# Autotuning/Eval #
###################

#def gen_conv2d_relay():
#    IN_DTYPE = 'int8'
#    OUT_DTYPE = 'int32'
#    N, H, W, CO, CI = 1, 32, 32, 16, 3
#    KH, KW = 5, 5
#    STRIDES, PADDING, DILATION = (1, 1), (2, 2), (1, 1)
#    KERNEL_SIZE = (KH, KW)
#    DATA_SHAPE = (N, CI, H, W)
#    KERNEL_SHAPE = (CO, CI, KH, KW)
#    BIAS_SHAPE = (CO,)
#    OUTPUT_SHAPE = (N, H, W, CO)
#
#    #assert False, "we might need to use NCHW for micro and interp, because the bias add causes problems"
#    # Construct Relay program (used for micro and interpreter eval).
#    data_var = relay.var("data", shape=DATA_SHAPE, dtype=IN_DTYPE)
#    kernel_var = relay.var("kernel", shape=KERNEL_SHAPE, dtype=IN_DTYPE)
#    bias_var = relay.var("bias", shape=BIAS_SHAPE, dtype=OUT_DTYPE)
#    conv_expr = relay.nn.conv2d(
#            data_var, kernel_var,
#            kernel_size=KERNEL_SIZE,
#            strides=STRIDES,
#            padding=PADDING,
#            dilation=DILATION,
#            channels=CO,
#            data_layout=LAYOUT,
#            out_layout=LAYOUT,
#            out_dtype=OUT_DTYPE)
#    func = relay.Function(relay.analysis.free_vars(conv_expr), conv_expr)
#    mod = relay.Module.from_expr(func)
#    mod = transform.InferType()(mod)
#    return mod

def get_num_devices(dev_id):
    conn = rpc.connect_tracker(TRACKER_ADDR, TRACKER_PORT)
    summary = conn.text_summary()
    num_connected = 0
    for line in summary.split('\n'):
        if 'Queue Status' in line:
            break
        if dev_id in line:
            num_connected += 1
    return num_connected


def tune_model(tasks, log_file_name):
    if N_PARALLEL is None:
        n_parallel = get_num_devices(DEVICE_ID)
    else:
        n_parallel = N_PARALLEL

    print('[Tuning]')

    build_func = tvm.micro.cross_compiler(
        DEV_CONFIG,
        micro.LibType.OPERATOR,
        lib_headers=MICRO_HEADERS,
        lib_include_paths=MICRO_INCLUDE_PATHS)
    runner = autotvm.RPCRunner(
        DEVICE_ID,
        TRACKER_ADDR,
        TRACKER_PORT,
        n_parallel=n_parallel,
        number=N_PER_TRIAL,
        timeout=TIMEOUT)
    measure_option = autotvm.measure_option(
        builder=autotvm.LocalBuilder(build_func=build_func),
        runner=runner)

    # create tmp log file
    tmp_log_file = log_file_name + '.tmp'
    # if os.path.exists(tmp_log_file):
    #     os.remove(tmp_log_file)

    for i, task in enumerate(tasks):
        #input(f'starting task {i}: ({task.name}, {task.args})')
        prefix = "[Task %2d/%2d] " % (i+1, len(tasks))
        #tuner = XGBTuner(task, loss_type='rank')
        tuner = GATuner(task)

        # start tuning
        tuner.tune(n_trial=min(N_TRIAL, len(task.config_space)),
                early_stopping=EARLY_STOPPING,
                measure_option=measure_option,
                callbacks=[
                    autotvm.callback.progress_bar(N_TRIAL, prefix=prefix),
                    autotvm.callback.log_to_file(tmp_log_file)])

    #print("\nBest configs:")
    #for i, task in enumerate(reversed(tasks)):
    #    # show best config from tuning
    #    dispatch_context = autotvm.apply_history_best(E2E_LOG_FILE_NAME)
    #    best_config = dispatch_context.query(task.target, task.workload)
    #    print(f'  task.target: {task.target}')
    #    print(f'  task {i}: {best_config}')

    # store best record in a cache file
    #autotvm.record.pick_best(tmp_log_file, log_file_name)
    custom_pick_best(tmp_log_file, log_file_name, top_k=5)
    os.remove(tmp_log_file)


# def eval_model(mod, target):
#     with micro.Session(DEV_CONFIG) as sess:
#         graph_mod = relay_micro_build(mod['main'], DEV_CONFIG, target)
#         ctx = tvm.micro_dev(0)

#         data_shape = list(map(lambda x: x.value, mod['main'].params[0].checked_type.shape))
#         data_tvm = tvm.nd.array(
#             (np.random.uniform(size=data_shape)).astype(IN_DTYPE), ctx)
#         kernel_shape = list(map(lambda x: x.value, mod['main'].params[1].checked_type.shape))
#         kernel_tvm = tvm.nd.array(
#             (np.random.uniform(size=kernel_shape)).astype(IN_DTYPE), ctx)

#         graph_mod.set_input(key='data', value=data_tvm)
#         graph_mod.set_input(key='kernel', value=kernel_tvm)

#         # evaluate
#         print("Evaluate inference time cost...")
#         # clear any previous batch times
#         ctx.sync()
#         sess.get_last_batch_time()
#         results = []
#         for _ in range(N_PER_TRIAL):
#             graph_mod.run()
#             ctx.sync()
#             results.append(sess.get_last_batch_time())
#         return np.mean(results), np.std(results)


def update_rpc_server_dev_cfg(template_key):
    if 'MICRO_RPC_SERVER_DEV_CONFIG_BASE' not in os.environ:
        # TODO: switch to logging
        print('WARNING: `RPC_SERVER_DEV_CONFIG_BASE` not in environment. RPC server config will not be auto-updated')
        input('[press enter to continue]')
        return
    # each op strategy needs a slightly different memory layout, so we update
    # the dev config the RPC servers use (only works if the script that restarts the RPC
    # server upon file modification is used)
    if template_key == 'direct':
        DEV_CONFIG['mem_layout'] = micro.device.arm.stm32f746xx.gen_mem_layout(OrderedDict([
            ('text', (18000, MemConstraint.ABSOLUTE_BYTES)),
            ('rodata', (100, MemConstraint.ABSOLUTE_BYTES)),
            ('data', (100, MemConstraint.ABSOLUTE_BYTES)),
            ('bss', (600, MemConstraint.ABSOLUTE_BYTES)),
            ('args', (4096, MemConstraint.ABSOLUTE_BYTES)),
            ('heap', (100.0, MemConstraint.WEIGHT)),
            #('workspace', (132000, MemConstraint.ABSOLUTE_BYTES)),
            ('workspace', (13000, MemConstraint.ABSOLUTE_BYTES)),
            ('stack', (128, MemConstraint.ABSOLUTE_BYTES)),
        ]))
    elif template_key == 'direct_simd':
        DEV_CONFIG['mem_layout'] = micro.device.arm.stm32f746xx.gen_mem_layout(OrderedDict([
            ('text', (18000, MemConstraint.ABSOLUTE_BYTES)),
            ('rodata', (100, MemConstraint.ABSOLUTE_BYTES)),
            ('data', (100, MemConstraint.ABSOLUTE_BYTES)),
            ('bss', (600, MemConstraint.ABSOLUTE_BYTES)),
            ('args', (4096, MemConstraint.ABSOLUTE_BYTES)),
            ('heap', (100.0, MemConstraint.WEIGHT)),
            ('workspace', (13000, MemConstraint.ABSOLUTE_BYTES)),
            ('stack', (128, MemConstraint.ABSOLUTE_BYTES)),
        ]))
    elif template_key == 'partial_im2col':
        DEV_CONFIG['mem_layout'] = micro.device.arm.stm32f746xx.gen_mem_layout(OrderedDict([
            ('text', (18000, MemConstraint.ABSOLUTE_BYTES)),
            ('rodata', (100, MemConstraint.ABSOLUTE_BYTES)),
            ('data', (100, MemConstraint.ABSOLUTE_BYTES)),
            ('bss', (600, MemConstraint.ABSOLUTE_BYTES)),
            ('args', (4096, MemConstraint.ABSOLUTE_BYTES)),
            ('heap', (100.0, MemConstraint.WEIGHT)),
            ('workspace', (132000, MemConstraint.ABSOLUTE_BYTES)),
            # ('workspace', (64000, MemConstraint.ABSOLUTE_BYTES)),
            ('stack', (128, MemConstraint.ABSOLUTE_BYTES)),
        ]))
    else:
        assert False

    dev_config_base = os.environ['MICRO_RPC_SERVER_DEV_CONFIG_BASE']
    for i in range(10):
        DEV_CONFIG['server_port'] = 6666 + i
        with open(f'{RPC_SERVER_DEV_CONFIG_BASE}/{i}/utvm_dev_config.json', 'w') as f:
            json.dump(DEV_CONFIG, f, indent=4)


def get_tasks(template_key):
    from tvm.autotvm.task.topi_integration import TaskExtractEnv
    TaskExtractEnv()

    if template_key == 'direct':
        @autotvm.task.register('topi_nn_conv2d', override=True)
        def _conv2d_direct(*args, **kwargs):
            return conv2d_direct(*args, **kwargs)
        data_layout = conv2d_direct.default_data_layout
        kernel_layout = conv2d_direct.default_kernel_layout
    elif template_key == 'direct_simd':
        # @autotvm.task.register('topi_nn_conv2d', override=True)
        # def _conv2d_direct_simd(*args, **kwargs):
        #     return conv2d_direct_simd(*args, **kwargs)
        data_layout = conv2d_direct_simd.default_data_layout
        kernel_layout = conv2d_direct_simd.default_kernel_layout
    elif template_key == 'partial_im2col':
        # @autotvm.task.register('topi_nn_conv2d', override=True)
        # def _conv2d_partial_im2col(*args, **kwargs):
        #     return conv2d_partial_im2col(*args, **kwargs)
        data_layout = conv2d_partial_im2col.default_data_layout
        kernel_layout = conv2d_partial_im2col.default_kernel_layout
    else:
        assert False

    #from mxnet.gluon.model_zoo.vision import get_model
    #block = get_model('mobilenetv2_0.25', pretrained=True)
    #mod, params = relay.frontend.from_mxnet(block, shape={'data': INPUT_SHAPE}, dtype=DTYPE)

    #mod, params = gen_conv2d('NHWC', 'HWOI')

    mod, params = gen_cifar10_cnn(
        data_layout, kernel_layout, input_op_strategy=template_key, use_random_params=True)

    tasks = collect_conv_tasks(mod['main'], TARGET, template_key)

    # dumb_tasks = autotvm.task.extract_from_program(
    #     mod['main'], target=TARGET, params=params, ops=TUNE_OPS)
    print(f'extracted {len(tasks)} tasks')
    assert len(tasks) == 3

    # for i in range(len(tasks)):
    #     assert 'conv2d' in tasks[i].name
    #     # overwrite template key (defaults to 'direct') with the desired key
    #     tasks[i] = autotvm.task.create(
    #             tasks[i].name,
    #             tasks[i].args,
    #             tasks[i].target,
    #             tasks[i].target_host,
    #             template_key=template_key)

    return tasks


def main():
    # TEMPLATE_KEYS = ['direct', 'direct_simd', 'partial_im2col']
    TEMPLATE_KEYS = ['direct']

    for template_key in TEMPLATE_KEYS:
        log_file_name = f'{DEVICE_ID}.{template_key}.e2e.log'

        update_rpc_server_dev_cfg(template_key)
        tasks = get_tasks(template_key)

        tune_model(tasks, log_file_name)


if __name__ == '__main__':
    main()
    #assert False, "Task extraction is stateful and whichever eval is run first sets the schedule to be used on subsequent evals"
