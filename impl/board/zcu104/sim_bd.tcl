# sim_bd.tcl - run the Block Design testbench (sim/tb_design_1.sv) in XSim, behavioural.
#   vivado -mode batch -nolog -nojournal -source impl/board/zcu104/sim_bd.tcl
# Needs the project from build_bd.tcl (bd_only is enough). Exit status 0 only on "TB_RESULT PASS".

set script_dir [file normalize [file dirname [info script]]]
set xpr [file join $script_dir build uart_apb_zcu104 uart_apb_zcu104.xpr]
if {![file exists $xpr]} {
    puts "ERROR: $xpr not found: run build_bd.tcl first (-tclargs bd_only is enough)"
    exit 1
}
open_project $xpr

set tb [file join $script_dir sim tb_design_1.sv]
if {[llength [get_files -quiet -of_objects [get_filesets sim_1] $tb]] == 0} {
    add_files -fileset sim_1 -norecurse $tb
}
set_property top tb_design_1 [get_filesets sim_1]
set_property top_lib xil_defaultlib [get_filesets sim_1]
set_property -name {xsim.simulate.runtime} -value {all} -objects [get_filesets sim_1]
update_compile_order -fileset sim_1

launch_simulation -simset sim_1 -mode behavioral
close_sim -quiet

set log [file join [get_property DIRECTORY [current_project]] uart_apb_zcu104.sim sim_1 behav xsim simulate.log]
if {![file exists $log]} { puts "ERROR: no simulate.log at $log"; exit 1 }
set fh [open $log r]; set txt [read $fh]; close $fh
foreach line [split $txt "\n"] {
    if {[regexp {^(TB_INFO|TB_CHECK_FAIL|TB_RESULT)} $line]} { puts $line }
}
if {[regexp -line {^TB_RESULT PASS} $txt]} {
    puts "BD testbench PASS (log: $log)"
} else {
    puts "ERROR: BD testbench did not pass (log: $log)"
    exit 1
}
