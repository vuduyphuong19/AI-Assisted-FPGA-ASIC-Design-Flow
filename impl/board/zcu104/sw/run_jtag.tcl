# run_jtag.tcl - program the ZCU104 over JTAG and run the uart_apb demo (Vitis 2022.2 XSCT)
#   ZCU104 powered, USB JTAG cable connected, boot mode switch set to JTAG (UG1267, boot modes).
#   xsct impl/board/zcu104/sw/run_jtag.tcl
# Uses: the bitstream from build_bd.tcl (full run), psu_init.tcl and the ELF from build_sw.tcl.

set here  [file normalize [file dirname [info script]]]
set build [file normalize [file join $here .. build]]
set ws    [file join $build vitis_ws]

proc need {what pattern} {
    set f [lindex [lsort [glob -nocomplain {*}$pattern]] end]
    if {$f eq ""} { puts "ERROR: $what not found ($pattern)"; exit 1 }
    puts "$what: $f"
    return $f
}
set bit  [need bitstream [list [file join $build uart_apb_zcu104 uart_apb_zcu104.runs impl_1 *.bit]]]
set init [need psu_init  [list [file join $ws * hw psu_init.tcl] [file join $ws * export * hw psu_init.tcl]]]
set elf  [need ELF       [list [file join $ws uart_apb_demo Debug uart_apb_demo.elf]]]

connect
# make the A53 cores visible to the debugger, reset the system
targets -set -nocase -filter {name =~ "*PSU*"}
mwr 0xffca0038 0x1ff
rst -system
after 1000

# program the PL
targets -set -nocase -filter {name =~ "*PSU*"}
fpga -file $bit

# PS init (clocks, DDR, MIO) from the Vivado PS configuration, then release PS-PL isolation
targets -set -nocase -filter {name =~ "*PSU*"}
source $init
psu_init
after 1000
psu_ps_pl_isolation_removal
after 1000
psu_ps_pl_reset_config

# load and run the demo on A53 #0
targets -set -nocase -filter {name =~ "*A53*#0"}
rst -processor
dow $elf
con
puts "Running. PS console and the uart_apb line both at 115200 8N1."
