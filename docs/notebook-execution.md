# Notebook execution

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/notebook-execution.md)

## Scope and behavior

`notebook_execute` is not in the default tool list. Set
`AGENT_TOOL_EXPOSE=notebook_execute` to offer it to the model; without it,
notebooks run through `execute_shell` in the kernel's own environment.

`notebook_execute` reuses Python execution approval, the selected Local/Docker
sandbox and detached process supervisor. It adds selection by inclusive 1-based
cell range (counting markdown cells too), optional skipped cells, per-cell timeout,
and saved notebook outputs. It does not install packages automatically.

Each call starts a fresh kernel. Earlier cells are not implicitly executed, so
select from cell 1 when later code depends on initialization. `kernel_name` names
an installed Jupyter kernelspec, not a Python executable. Execution uses the source
notebook directory as the working directory.

The destination must be a new `.ipynb` file; the default has a random suffix.
Existing destinations and the source path are refused before kernel startup.
The output copy clears old outputs, including unselected cells, and records
selection/status in `metadata.astra_execution`. Source bytes are unchanged by the
runner (notebook code itself has the normal permissions of its execution environment).

Each cell boundary saves an atomic output copy and emits JSON progress to the
process log. Errors and timeouts stop execution and preserve partial output.
A hard process cancellation may interrupt the current cell before its output is
saved; the most recent saved boundary remains. No persistent-kernel resume is
claimed. Normal kernel shutdown and timeout handling use nbclient.

## Usage

Install the optional extra in the selected execution environment:

```sh
pip install -e '.[notebook]'
```

Example tool arguments:

```json
{"path":"work.ipynb","end_cell":10,"skip_cells":[7],"cell_timeout":600,"background":true}
```

Use the returned `process_id` with `process_poll`, `process_read` and
`process_cancel`. Notebook stdout/plots are in the output notebook; process logs
contain cell progress and execution diagnostics.

## Validation

Real local kernels exercise range/skip behavior, shared variables within one run,
source preservation, failure output preservation, and timeout saving. An actual
registry → detached supervisor → kernel test verifies background polling, logs
and saved cell output. Separate zero/nonzero exit tests ensure stderr diagnostics
do not incorrectly classify a successful process as failed.

Docker forwarding reuses the existing Python sandbox route; this change does not
claim a fresh Docker or coursework notebook acceptance run.

The execution foundation follows the official
[nbclient documentation](https://nbclient.readthedocs.io/en/latest/client.html).
