# zcu104_uart.xdc - board-level implementation constraints: uart_apb in the ZCU104 Block Design
# (top = design_1_wrapper). Pins are in zcu104_uart_pins.xdc.
#
# Clocks: none here. The block runs on PS pl_clk0 (100 MHz, spec ppa_targets.fmax_mhz); the
# zynq_ultra_ps_e IP's own XDC creates that clock. Do NOT add impl/uart_apb.xdc to this project.
# It is the out-of-context file and constrains a top-level `clk` port that does not exist here.
# In this context the APB paths (bridge -> uart_apb comb prdata/pslverr -> bridge) are internal
# register-to-register paths, timed at 100 MHz (closes review finding F022 at board level).

# ---------------------------------------------------------------- asynchronous UART pins
# REQ-13: rxd is asynchronous to clk. It enters rx_meta_ff -> rx_sync_ff (ASYNC_REG, style R16),
# so there is no timing relationship to analyse from the pad.
set_false_path -from [get_ports rxd]
# REQ-9: txd changes at most once per D >= DIV_MIN = 4 clk cycles and the far end samples it
# asynchronously (mid-bit), so there is no output timing relationship to analyse to the pad.
set_false_path -to [get_ports txd]

# ---------------------------------------------------------------- electrical / robustness
# REQ-14: keep rxd at the idle level (1) when nothing drives it (cable unplugged, adapter off).
# Otherwise a floating input produces false start bits and FRAME_ERR.
set_property PULLUP TRUE [get_ports rxd]
# txd: slow edges and moderate drive are enough for <= 1 Mbaud and reduce ringing on the line.
set_property SLEW SLOW [get_ports txd]
set_property DRIVE 8   [get_ports txd]
# No IOB packing: txd is a false path (REQ-9), so a fixed clock-to-pad delay buys nothing, and
# IOB TRUE was not honoured in this BD build (Place 30-722). rxd must not be packed either:
# rx_meta_ff stays next to rx_sync_ff (ASYNC_REG) for metastability MTBF.

# ---------------------------------------------------------------- bitstream
set_property BITSTREAM.GENERAL.COMPRESS TRUE [current_design]
