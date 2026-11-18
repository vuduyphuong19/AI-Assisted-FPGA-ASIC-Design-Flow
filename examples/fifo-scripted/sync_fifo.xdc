# sync_fifo.xdc — out-of-context constraints (impl-engineer, scripted)
# REQ: ppa_targets.fmax_mhz = 200 -> 5.000 ns
create_clock -name clk -period 5.000 [get_ports clk]
set_property HD.CLK_SRC BUFGCTRL_X0Y0 [get_ports clk]
# No timing exceptions: the spec documents no clock crossings or multicycle paths.
