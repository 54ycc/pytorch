import tempfile
import shutil
from string import Template
import unittest
import types

import torch
import torch.testing._internal.common_nn as common_nn
from torch.testing._internal.common_cuda import TEST_CUDA
from cpp_api_parity.utils import TorchNNModuleTestParams, CppArg
from cpp_api_parity import torch_nn_modules

# yf225 TODO: write better docs here
# Module implementation correctness check:

# Step 1: Translate ctor args from Python layer to C++ layer
# Step 2: Construct a C++ layer, run forward and backward on it, save all its params/buffers/gradients into a ScriptModule
# Step 3: Load that ScriptModule into Python, and compare output/params/buffers/gradients with Python layer (forward and backward)

# NN tests use double as the default dtype
torch.set_default_dtype(torch.double)

# yf225 TODO: move to common utils?
devices = ['cpu', 'cuda']

# yf225 TODO: move to common utils?
TORCH_NN_COMMON_TEST_HARNESS = """
#include <torch/script.h>

void write_ivalue_to_file(const torch::IValue& ivalue, const std::string& file_path) {
  auto bytes = torch::jit::pickle_save(ivalue);
  std::ofstream fout(file_path, std::ios::out | std::ios::binary);
  fout.write(bytes.data(), bytes.size());
  fout.close();
}

c10::Dict<std::string, torch::Tensor> load_dict_from_file(const std::string& file_path) {
  c10::Dict<std::string, torch::Tensor> arg_dict;
  auto arg_dict_module = torch::jit::load(file_path);
  for (const auto& p : arg_dict_module.named_buffers(/*recurse=*/false)) {
    arg_dict.insert(p.name, p.value);
  }
  return arg_dict;
}

// Generates rand tensor with non-equal values. This ensures that duplicate
// values won't be causing test failure for modules like MaxPooling.
// size should be small, otherwise randperm fails / long overflows.
torch::Tensor _rand_tensor_non_equal(torch::IntArrayRef size) {
  int64_t total = 1;
  for (int64_t elem : size) {
    total *= elem;
  }
  return torch::randperm(total).view(size).to(torch::kDouble);
}
"""

'''
Expected substitutions:

${module_variant_name}
${module_qualified_name}
${cpp_tmp_folder}
${cpp_args_construction_stmts}
${cpp_constructor_args}
${device}
${cpp_forward_args_symbols}
'''
TORCH_NN_MODULE_TEST_FORWARD_BACKWARD = Template("""
void ${module_variant_name}_test_forward_backward() {
  pybind11::gil_scoped_release no_gil;

  // Declare arguments
  auto arg_dict = load_dict_from_file("${cpp_tmp_folder}/${module_variant_name}_arg_dict.pt");
  ${cpp_args_construction_stmts};

  // Construct module and load params/buffers from Python module
  ${module_qualified_name} module${cpp_constructor_args};
  torch::load(module, "${cpp_tmp_folder}/${module_variant_name}_module.pt");
  module->to(std::string("${device}"));

  // Some modules (such as `RReLU`) create random tensors in their forward pass.
  // To make sure the random tensors created are the same in Python/C++, we need
  // to set the RNG seed manually.
  torch::manual_seed(0);

  // Forward pass
  auto cpp_output = module(${cpp_forward_args_symbols});

  // Save the output into a file to be compared in Python later
  write_ivalue_to_file(
    torch::IValue(cpp_output),
    "${cpp_tmp_folder}/${module_variant_name}_forward_output.pt");

  // Backward pass
  cpp_output.sum().backward();

  // Put all gradients into a c10::Dict, save it into a file to be compared in Python later
  c10::Dict<std::string, torch::Tensor> grad_dict;
  for (const auto& param : module->named_parameters()) {
    torch::Tensor grad = param.value().grad();
    if (grad.is_sparse()) {
      grad = grad.to_dense();
    }
    grad_dict.insert(param.key() + "_grad", grad);
  }

  write_ivalue_to_file(
    torch::IValue(grad_dict),
    "${cpp_tmp_folder}/${module_variant_name}_backward_grad_dict.pt");
}
""")

# yf225 TODO: move to common utils?
def compile_cpp_code_inline(name, cpp_sources, functions):
  cpp_module = torch.utils.cpp_extension.load_inline(
    name=name,
    cpp_sources=cpp_sources,
    functions=functions,
    verbose=False,
  )
  return cpp_module

# yf225 TODO: move to common utils
def convert_to_list(python_input):
  if isinstance(python_input, torch.Tensor):
    return [python_input]
  else:
    return [tensor for tensor in python_input]

# yf225 TODO: move to common utils
def set_python_tensors_requires_grad(python_tensors):
  return [tensor.requires_grad_(True) if tensor.dtype != torch.long else tensor for tensor in python_tensors]

# yf225 TODO: move to common utils
def move_python_tensors_to_device(python_tensors, device):
  return [tensor.to(device) for tensor in python_tensors]

def run_python_forward_backward(unit_test_class, test_params):
  device = test_params.device
  module = test_params.test_instance.constructor(*test_params.test_instance.constructor_args).to(device)

  inputs = set_python_tensors_requires_grad([arg_value for _, arg_value in test_params.arg_dict['input']])
  inputs = inputs + [arg_value for _, arg_value in test_params.arg_dict['target']]
  inputs = inputs + [arg_value for _, arg_value in test_params.arg_dict['extra_args']]
  inputs = move_python_tensors_to_device(inputs, device)

  # Some modules (such as `RReLU`) create random tensors in their forward pass.
  # To make sure the random tensors created are the same in Python/C++, we need
  # to set the RNG seed manually.
  torch.manual_seed(0)
  python_output = module(*inputs)

  # NOTE: This is a workaround to allow any module to be traced.
  # We can do this because we are only interested in transferring
  # the Python module's parameters and buffers to the C++ module.
  module.forward = types.MethodType(lambda self, input: input, module)
  script_module = torch.jit.trace(module, torch.tensor(0))

  python_output.sum().backward()
  # Put all gradients into a dict, to be compared later
  python_grad_dict = {}
  for name, param in module.named_parameters():
    grad = param.grad;
    if grad.is_sparse:
      grad = grad.to_dense()
    python_grad_dict[name + "_grad"] = grad

  return script_module, python_output, python_grad_dict

def test_forward_backward(unit_test_class, test_params):
  module_variant_name = test_params.module_variant_name

  # Run forward and backward on Python module
  script_module, python_output, python_grad_dict = run_python_forward_backward(unit_test_class, test_params)

  # Save Python module and arguments to be used from C++ function
  script_module.save("{}/{}_module.pt".format(test_params.cpp_tmp_folder, module_variant_name))
  arg_dict_flat = {
    arg_name: arg_value \
      for arg_name, arg_value in \
        test_params.arg_dict['input'] + \
        test_params.arg_dict['target'] + \
        test_params.arg_dict['extra_args'] + \
        test_params.arg_dict['other']
  }
  arg_dict_module = torch.nn.Module()
  for arg_name, arg_value in arg_dict_flat.items():
    assert isinstance(arg_value, torch.Tensor)
    arg_dict_module.register_buffer(arg_name, arg_value)
  torch.jit.script(arg_dict_module).save("{}/{}_arg_dict.pt".format(test_params.cpp_tmp_folder, module_variant_name))

  cpp_test_name = '{}_{}'.format(test_params.module_variant_name, 'test_forward_backward')
  cpp_test_fn = getattr(unit_test_class.module_impl_check_cpp_module, cpp_test_name)

  def run_cpp_test_fn_and_check_output():
    cpp_test_fn()
    cpp_output = torch.load("{}/{}_forward_output.pt".format(test_params.cpp_tmp_folder, module_variant_name))
    cpp_grad_dict = torch.load("{}/{}_backward_grad_dict.pt".format(test_params.cpp_tmp_folder, module_variant_name))

    def generate_error_msg(name, cpp_value, python_value):
      return "Parity test failed: {} in C++ has value: {}, which does not match the corresponding value in Python: {}".format(
        name, cpp_value, python_value)

    # Check that forward outputs are equal
    unit_test_class.assertTrue(
      torch.allclose(python_output, cpp_output),
      generate_error_msg("forward output", cpp_output, python_output))

    # Check that module parameter gradients are equal after backward pass
    unit_test_class.assertEqual(
      len(python_grad_dict), len(cpp_grad_dict),
      generate_error_msg("# of parameters", len(cpp_grad_dict), len(python_grad_dict)))
    for key in python_grad_dict:
      unit_test_class.assertTrue(
        key in cpp_grad_dict,
        generate_error_msg("\"Does module have a parameter named `{}`?\"".format(key[:-5]), False, True))
      unit_test_class.assertTrue(
        torch.allclose(python_grad_dict[key], cpp_grad_dict[key]),
        generate_error_msg("gradient of `{}`".format(key[:-5]), cpp_grad_dict[key], python_grad_dict[key]))

  if not test_params.has_parity:
    with unit_test_class.assertRaisesRegex(AssertionError, "Parity test failed"):
      run_cpp_test_fn_and_check_output()
  else:
    run_cpp_test_fn_and_check_output()

  # Remove temporary folder that stores C++ outputs
  shutil.rmtree(test_params.cpp_tmp_folder)

def test_torch_nn_module_variant(unit_test_class, test_params):
  test_forward_backward(unit_test_class, test_params)

# yf225 TODO: move to common utils?
def compute_module_name(test_params_dict):
    fullname = test_params_dict.get('fullname', None)
    if fullname:
        # NOTE: This doesn't work for some of the `wrap_functional` module tests such as "interpolate_nearest_1d",
        # because in that case the module `interpolate` is not in `torch.nn` but rather in `torch.nn.functional`.
        # We will fix this when we have parity tests for `torch.nn.functional` modules.
        module_name = fullname.split('_')[0]
    else:
        module_name = test_params_dict.get('module_name')
    return module_name

# yf225 TODO: move to common utils?
def process_test_params_for_module(test_params_dict, device, test_instance_class):
  module_name = compute_module_name(test_params_dict)
  test_params_dict['constructor'] = test_params_dict.get('constructor', getattr(torch.nn, module_name))
  test = test_instance_class(**test_params_dict)
  # yf225 TODO: can we remove the magic number `5` here?
  module_variant_name = test.get_name()[5:] + (('_' + device) if device != 'cpu' else '')

  arg_dict = {
    'input': [],
    'target': [],
    'extra_args': [],
    'other': [],
  }

  def put_args_into_arg_dict(arg_type, arg_type_prefix, args):
    for i, arg in enumerate(args):
      arg_dict[arg_type].append(CppArg(name=arg_type_prefix+str(i), value=arg))

  put_args_into_arg_dict('input', 'i', convert_to_list(test._get_input()))
  if is_criterion_test(test):
    put_args_into_arg_dict('target', 't', convert_to_list(test._get_target()))
  if test.extra_args:
    put_args_into_arg_dict('extra_args', 'e', convert_to_list(test.extra_args))

  cpp_arg_symbol_map = test_params_dict.get('cpp_arg_symbol_map', {})
  for arg_name, arg_value in cpp_arg_symbol_map.items():
    if isinstance(arg_value, str):
      if arg_value == 'input':
        arg_dict['other'].append(CppArg(name=arg_name, value=test._get_input()))
      else:
        raise RuntimeError("`{}` has unsupported string value: {}".format(arg_name, arg_value))
    elif isinstance(arg_value, torch.Tensor):
      arg_dict['other'].append(CppArg(name=arg_name, value=arg_value))
    else:
      raise RuntimeError("`{}` has unsupported value: {}".format(arg_name, arg_value))

  return TorchNNModuleTestParams(
    module_name=module_name,
    module_variant_name=module_variant_name,
    test_instance=test,
    cpp_constructor_args=test_params_dict.get('cpp_constructor_args', ''),
    arg_dict=arg_dict,
    has_parity=test_params_dict.get('has_parity', True),
    device=device,
    cpp_tmp_folder=tempfile.mkdtemp(),
  )

# yf225 TODO: move to common utils?
def has_test(unit_test_class, test_name):
  return hasattr(unit_test_class, test_name)

# yf225 TODO: move to common utils?
def add_test(unit_test_class, test_name, test_fn):
  if has_test(unit_test_class, test_name):
    raise RuntimeError("Found two tests with the same name: " + test_name)
  setattr(unit_test_class, test_name, test_fn)

# yf225 TODO: move to common utils?
def set_cpp_tensors_requires_grad(cpp_tensor_stmts, cpp_tensors):
  assert len(cpp_tensor_stmts) == len(cpp_tensors)
  return ['{}.requires_grad_(true)'.format(tensor_stmt) if tensor.dtype != torch.long else tensor_stmt \
    for tensor_stmt, (_, tensor) in zip(cpp_tensor_stmts, cpp_tensors)]

# yf225 TODO: move to common utils
def move_cpp_tensors_to_device(cpp_tensor_stmts, device):
  return ['{}.to("{}")'.format(tensor_stmt, device) for tensor_stmt in cpp_tensor_stmts]

def is_criterion_test(test_instance):
  return isinstance(test_instance, common_nn.CriterionTest) or \
    isinstance(test_instance, common_nn.NewCriterionTest)


torch_nn_test_params_map = {}

def add_torch_nn_module_impl_parity_tests(parity_table, unit_test_class, test_params_dicts, test_instance_class):
  for test_params_dict in test_params_dicts:
    # Skip all `torch.nn.functional` tests, since they are handled by another test suite.
    if 'FunctionalModule' in str(test_params_dict.get('constructor', '')):
      continue

    module_name = compute_module_name(test_params_dict)

    assert hasattr(torch.nn, module_name), \
      "`torch.nn` doesn't have module `{}`. ".format(module_name) + \
      "If you are adding a new test, please set `fullname` using format `ModuleName_desc`, " + \
      "or set `module_name` using format `ModuleName`."

    module_full_name = 'torch::nn::' + module_name
    
    assert module_full_name in parity_table['torch::nn'], \
      "Please add `{}` entry to `torch::nn` section of `test/cpp_api_parity/parity-tracker.md`.".format(module_full_name)

    has_impl_parity, _ = parity_table['torch::nn'][module_full_name]

    for device in devices:
      test_params = process_test_params_for_module(
        test_params_dict=test_params_dict,
        device=device,
        test_instance_class=test_instance_class,
      )
      test_name = 'test_torch_nn_{}'.format(test_params.module_variant_name)
      torch_nn_test_params_map[test_name] = test_params

      def test_fn(self):
        test_torch_nn_module_variant(unit_test_class=self, test_params=torch_nn_test_params_map[self._testMethodName])

      if device == 'cuda':
        test_fn = unittest.skipIf(not TEST_CUDA, "CUDA unavailable")(test_fn)
        test_fn = unittest.skipIf(not test_params_dict.get('test_cuda', True), "Excluded from CUDA tests")(test_fn)

      # If `Implementation Parity` entry in parity table for this module is `No`,
      # we mark the test as expected failure.
      if not has_impl_parity:
        test_fn = unittest.expectedFailure(test_fn)

      add_test(unit_test_class, test_name, test_fn)


def add_tests(unit_test_class, test_params_dicts, test_instance_class, parity_table):
  add_torch_nn_module_impl_parity_tests(
    parity_table=parity_table,
    unit_test_class=unit_test_class,
    test_params_dicts=test_params_dicts,
    test_instance_class=test_instance_class)

# yf225 TODO: move to common utils?
# yf225 TODO: we should check in a copy of the generated source code, and then run consistency test (compare old vs. newly generated)
def generate_test_cpp_sources(test_params, template):
  device = test_params.device

  cpp_constructor_args = test_params.cpp_constructor_args
  if cpp_constructor_args != '':
    cpp_constructor_args = '({})'.format(cpp_constructor_args)

  # Build the list of arguments needed for module forward
  cpp_forward_args_symbols = []

  def add_cpp_forward_args(args):
    args_stmts = []
    for arg_name, _ in args:
      args_stmts.append('auto {} = arg_dict.at("{}")'.format(arg_name, arg_name))
      cpp_forward_args_symbols.append(arg_name)
    return args_stmts

  cpp_forward_input_args_stmts = move_cpp_tensors_to_device(set_cpp_tensors_requires_grad(add_cpp_forward_args(test_params.arg_dict['input']), test_params.arg_dict['input']), device)
  cpp_forward_target_args_stmts = move_cpp_tensors_to_device(add_cpp_forward_args(test_params.arg_dict['target']), device)
  cpp_forward_extra_args_stmts = move_cpp_tensors_to_device(add_cpp_forward_args(test_params.arg_dict['extra_args']), device)

  # Build the list of other arguments needed
  cpp_other_args_stmts = []
  for arg_name, _ in test_params.arg_dict['other']:
    cpp_other_args_stmts.append('auto {} = arg_dict.at("{}")'.format(arg_name, arg_name))
  cpp_other_args_stmts = move_cpp_tensors_to_device(cpp_other_args_stmts, device)
  
  cpp_args_construction_stmts = cpp_forward_input_args_stmts + cpp_forward_target_args_stmts + cpp_forward_extra_args_stmts + cpp_other_args_stmts

  test_cpp_sources = template.substitute(
    module_variant_name=test_params.module_variant_name,
    module_qualified_name='torch::nn::{}'.format(test_params.module_name),
    cpp_args_construction_stmts=";\n  ".join(cpp_args_construction_stmts),
    cpp_constructor_args=cpp_constructor_args,
    cpp_forward_args_symbols=", ".join(cpp_forward_args_symbols),
    cpp_tmp_folder=test_params.cpp_tmp_folder,
    device=device,
  )
  return test_cpp_sources

def build_cpp_tests(unit_test_class, print_cpp_source=False):
  # Put all cpp source code into one file and compile together, in order to speed up the build
  # yf225 TODO bonus point: check in the cpp source code for comparison
  if len(torch_nn_test_params_map) > 0:
    cpp_sources = TORCH_NN_COMMON_TEST_HARNESS
    functions = []
    modules_added_metadata_cpp_sources = set()
    for test_name, test_params in torch_nn_test_params_map.items():
      if not test_params.module_name in modules_added_metadata_cpp_sources:
        cpp_sources += torch_nn_modules.module_metadata_map.get(test_params.module_name, torch_nn_modules.TorchNNModuleMetadata()).cpp_sources
        modules_added_metadata_cpp_sources.add(test_params.module_name)
      cpp_sources += generate_test_cpp_sources(test_params=test_params, template=TORCH_NN_MODULE_TEST_FORWARD_BACKWARD)
      functions.append('{}_{}'.format(test_params.module_variant_name, 'test_forward_backward'))
    if print_cpp_source:
      print(cpp_sources)

    cpp_module = compile_cpp_code_inline(
      name='module_impl_check',
      cpp_sources=cpp_sources,
      functions=functions)
    unit_test_class.module_impl_check_cpp_module = cpp_module