# list_board_pins.tcl - print ZCU104 board-file pins that are candidates for txd/rxd
# (UART and PMOD), with their package pin and I/O standard as the board file states them.
#   vivado -mode batch -nolog -nojournal -source impl/board/zcu104/list_board_pins.tcl
# Read-only: opens no project, changes nothing.

set root [expr {[info exists ::env(XILINX_VIVADO)] ? $::env(XILINX_VIVADO) : ""}]
set files [glob -nocomplain [file join $root data xhub boards XilinxBoardStore boards Xilinx zcu104 * part0_pins.xml]]
if {[llength $files] == 0} {
    puts "ERROR: ZCU104 part0_pins.xml not found under $root (source <Vivado>/settings64.sh first)"
    exit 1
}
set f [lindex [lsort $files] end]
puts "Board file: $f"
set fh [open $f r]
set n 0
while {[gets $fh line] >= 0} {
    if {[regexp -nocase {<pin\M} $line] && [regexp -nocase {uart|pmod} $line]} {
        puts [string trim $line]
        incr n
    }
}
close $fh
puts "$n candidate pin(s). Fill zcu104_uart_pins.xdc with the pair you wire, and its iostandard."
