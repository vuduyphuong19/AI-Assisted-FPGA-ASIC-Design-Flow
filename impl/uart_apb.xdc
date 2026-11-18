# uart_apb.xdc - out-of-context constraints (source: spec ppa_targets.fmax_mhz = 100 -> 10.0 ns)
# Part: xczu7ev-ffvc1156-2-e (ZCU104). Single clock domain (clk = APB PCLK).

# Clocks
create_clock -name clk -period 10.000 [get_ports clk]
# OOC placement hint: a global clock buffer site that exists on xczu7ev (UltraScale+ has no
# BUFGCTRL_X0Y0; BUFGCE sites start at X0Y96, queried with get_sites in Vivado 2022.2). F010.
set_property HD.CLK_SRC BUFGCE_X0Y96 [get_ports clk]

# I/O delays: none (spec gives no external timing; TIMING-18 expected out of context).

# Exceptions: none. rxd is asynchronous to clk but is handled by the 2-flop ASYNC_REG
# synchronizer in RTL (spec port note); no false path or clock group is constrained.
