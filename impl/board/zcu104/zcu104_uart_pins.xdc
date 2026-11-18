# zcu104_uart_pins.xdc - package pins for txd / rxd on ZCU104.
#
# Source: Vivado 2022.2 board file
#   data/xhub/boards/XilinxBoardStore/boards/Xilinx/zcu104/1.1/
#   board.xml      interface "uart2_pl" (FT4232H USB-UART, PL channel), iostandard LVCMOS18
#   part0_pins.xml uart2_PL_TX loc C19 (FPGA drives -> FT4232H RX), uart2_PL_RX loc A20 (FT4232H TX -> FPGA)
# The FT4232H (USB ID 0403:6011, "Xilinx JTAG+3Serial") gives JTAG plus 3 serial ports on the host,
# over the same USB cable as the PS console. uart_apb is the PL channel: interface 3, /dev/ttyUSB3
# here (/dev/serial/by-id/*-if03-port0); the PS console is interface 1. Checked on the board.

set_property PACKAGE_PIN C19      [get_ports txd]
set_property PACKAGE_PIN A20      [get_ports rxd]
set_property IOSTANDARD  LVCMOS18 [get_ports {txd rxd}]
