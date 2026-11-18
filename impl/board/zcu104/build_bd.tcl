# build_bd.tcl - Vivado Block Design for uart_apb on ZCU104 (xczu7ev-ffvc1156-2-e)
#
#   source <Vivado 2022.2>/settings64.sh
#   cd <repo root>
#   vivado -mode batch -source impl/board/zcu104/build_bd.tcl                    ;# full: bitstream + XSA
#   vivado -mode batch -source impl/board/zcu104/build_bd.tcl -tclargs bd_only   ;# BD + synthesis + XSA (no bitstream)
#   vivado -mode gui   -source impl/board/zcu104/build_bd.tcl -tclargs bd_only   ;# open the BD in the GUI
# Then: sim_bd.tcl (BD testbench), sw/build_sw.tcl (Vitis app), sw/run_jtag.tcl (board).
#
# Block Design:
#   zynq_ultra_ps_e (board preset) M_AXI_HPM0_FPD -> smartconnect -> axi_apb_bridge (APB3)
#   -> uart_apb_bd (module reference: rtl/uart_apb.v) ; irq -> pl_ps_irq0 ; txd/rxd -> board pins
#   pl_clk0 = 100 MHz for every PL block; proc_sys_reset gives the synchronous active-low reset.
#   Address: UART at 0xA000_0000 (4 KB) in the A53 address map.
#
# Manual board integration: it does not produce mae evidence. The signed-off block evidence is the
# out-of-context flow (impl/uart_apb.xdc, EV-... in reports/signoff_report.md).

set script_dir [file normalize [file dirname [info script]]]
set repo       [file normalize [file join $script_dir .. .. ..]]
set build      [file join $script_dir build]
set proj       uart_apb_zcu104
set part       xczu7ev-ffvc1156-2-e
set bd_name    design_1
set bd_only    [expr {[lsearch -exact $argv bd_only] >= 0}]
set jobs       1                       ;# one run at a time: parallel IP synth runs exhaust a 16 GB desktop

proc fail {msg} { puts "ERROR: $msg"; exit 1 }
# latest installed version of a Xilinx IP (no hard-coded IP versions)
proc ipdef {name} {
    set v [lindex [lsort -dictionary [get_ipdefs -all -filter "VLNV =~ xilinx.com:ip:${name}:*"]] end]
    if {$v eq ""} { fail "IP xilinx.com:ip:$name not found in this Vivado installation" }
    return $v
}

# ---------------------------------------------------------------- pins must be real before impl
set pins_xdc [file join $script_dir zcu104_uart_pins.xdc]
set fh [open $pins_xdc r]; set pins_txt [read $fh]; close $fh
set pins_ok [expr {[regexp -line {^\s*set_property\s+PACKAGE_PIN\s+\S+\s+\[get_ports\s+txd\]} $pins_txt] &&
                   [regexp -line {^\s*set_property\s+PACKAGE_PIN\s+\S+\s+\[get_ports\s+rxd\]} $pins_txt] &&
                   [regexp -line {^\s*set_property\s+IOSTANDARD\s+\S+} $pins_txt]}]
if {!$pins_ok && !$bd_only} {
    fail "zcu104_uart_pins.xdc has no txd/rxd PACKAGE_PIN + IOSTANDARD yet. Fill it (see its header), or run with -tclargs bd_only."
}

# ---------------------------------------------------------------- project + board
create_project $proj [file join $build $proj] -part $part -force
set board [lindex [lsort [get_board_parts -quiet -latest_file_version *zcu104*]] end]
if {$board eq ""} { fail "ZCU104 board part not installed in this Vivado (Tools > Vivado Store > Boards)." }
set_property board_part $board [current_project]
puts "Board part: $board"

add_files -norecurse [list [file join $repo rtl uart_apb.v] [file join $script_dir uart_apb_bd.v]]
add_files -fileset constrs_1 -norecurse [list [file join $script_dir zcu104_uart.xdc] $pins_xdc]
update_compile_order -fileset sources_1

# ---------------------------------------------------------------- block design
create_bd_design $bd_name

set ps [create_bd_cell -type ip -vlnv [ipdef zynq_ultra_ps_e] zynq_ultra_ps_e_0]
apply_bd_automation -rule xilinx.com:bd_rule:zynq_ultra_ps_e -config {apply_board_preset "1"} $ps
set_property -dict [list \
    CONFIG.PSU__USE__M_AXI_GP0                 {1} \
    CONFIG.PSU__MAXIGP0__DATA_WIDTH            {32} \
    CONFIG.PSU__USE__M_AXI_GP1                 {0} \
    CONFIG.PSU__USE__M_AXI_GP2                 {0} \
    CONFIG.PSU__USE__IRQ0                      {1} \
    CONFIG.PSU__FPGA_PL0_ENABLE                {1} \
    CONFIG.PSU__CRL_APB__PL0_REF_CTRL__FREQMHZ {100} \
] $ps

set rst  [create_bd_cell -type ip -vlnv [ipdef proc_sys_reset] proc_sys_reset_0]
set sc   [create_bd_cell -type ip -vlnv [ipdef smartconnect] smartconnect_0]
set_property -dict [list CONFIG.NUM_SI {1} CONFIG.NUM_MI {1}] $sc
set apb  [create_bd_cell -type ip -vlnv [ipdef axi_apb_bridge] axi_apb_bridge_0]
set_property -dict [list CONFIG.C_APB_NUM_SLAVES {1} CONFIG.C_M_APB_PROTOCOL {apb3}] $apb
set uart [create_bd_cell -type module -reference uart_apb_bd uart_0]
set_property CONFIG.DIV_RESET {868} $uart

# clock: pl_clk0 (100 MHz) everywhere
set clk [get_bd_pins zynq_ultra_ps_e_0/pl_clk0]
connect_bd_net $clk [get_bd_pins zynq_ultra_ps_e_0/maxihpm0_fpd_aclk] \
                    [get_bd_pins proc_sys_reset_0/slowest_sync_clk] \
                    [get_bd_pins smartconnect_0/aclk] \
                    [get_bd_pins axi_apb_bridge_0/s_axi_aclk] \
                    [get_bd_pins uart_0/clk]
# reset: PS pl_resetn0 -> proc_sys_reset -> synchronous active-low resets
connect_bd_net [get_bd_pins zynq_ultra_ps_e_0/pl_resetn0] [get_bd_pins proc_sys_reset_0/ext_reset_in]
connect_bd_net [get_bd_pins proc_sys_reset_0/interconnect_aresetn] [get_bd_pins smartconnect_0/aresetn]
connect_bd_net [get_bd_pins proc_sys_reset_0/peripheral_aresetn] \
               [get_bd_pins axi_apb_bridge_0/s_axi_aresetn] [get_bd_pins uart_0/rst_n]
# bus: HPM0_FPD -> smartconnect -> AXI-Lite -> APB3 -> uart
connect_bd_intf_net [get_bd_intf_pins zynq_ultra_ps_e_0/M_AXI_HPM0_FPD] [get_bd_intf_pins smartconnect_0/S00_AXI]
connect_bd_intf_net [get_bd_intf_pins smartconnect_0/M00_AXI] [get_bd_intf_pins axi_apb_bridge_0/AXI4_LITE]
connect_bd_intf_net [get_bd_intf_pins axi_apb_bridge_0/APB_M] [get_bd_intf_pins uart_0/S_APB]
# interrupt and board pins
connect_bd_net [get_bd_pins uart_0/irq] [get_bd_pins zynq_ultra_ps_e_0/pl_ps_irq0]
create_bd_port -dir O txd
create_bd_port -dir I rxd
connect_bd_net [get_bd_ports txd] [get_bd_pins uart_0/txd]
connect_bd_net [get_bd_ports rxd] [get_bd_pins uart_0/rxd]

# address map: UART at 0xA000_0000, 4 KB
assign_bd_address
set segs [get_bd_addr_segs -quiet -of_objects [get_bd_addr_spaces zynq_ultra_ps_e_0/Data]]
if {[llength $segs] != 1} { fail "expected one address segment for the APB bridge, found: $segs" }
set_property offset 0xA0000000 $segs
set_property range  4K         $segs

validate_bd_design
regenerate_bd_layout
save_bd_design

set bd_file [get_files ${bd_name}.bd]
generate_target all $bd_file
set wrapper [make_wrapper -files $bd_file -top]
add_files -norecurse $wrapper
set_property top ${bd_name}_wrapper [current_fileset]
update_compile_order -fileset sources_1

# BD-level testbench (sim_1): the PS simulation model (VIP) is the bus master; run it with sim_bd.tcl
add_files -fileset sim_1 -norecurse [file join $script_dir sim tb_design_1.sv]
set_property top tb_design_1 [get_filesets sim_1]
set_property top_lib xil_defaultlib [get_filesets sim_1]
update_compile_order -fileset sim_1
set xsa [file join $build uart_apb_zcu104.xsa]

# ---------------------------------------------------------------- synthesis
launch_runs synth_1 -jobs $jobs
wait_on_run synth_1
if {[get_property PROGRESS [get_runs synth_1]] ne "100%"} { fail "synthesis failed: see [get_property DIRECTORY [get_runs synth_1]]/runme.log" }
if {$bd_only} {
    # pre-bitstream XSA: enough to build the software (sw/build_sw.tcl); re-exported with the bitstream later
    write_hw_platform -fixed -force -file $xsa
    puts "BD + synthesis done (bd_only). Project: [file join $build $proj $proj.xpr]; XSA without bitstream: $xsa"
    return
}

# ---------------------------------------------------------------- implementation + bitstream
launch_runs impl_1 -to_step write_bitstream -jobs $jobs
wait_on_run impl_1
if {[get_property PROGRESS [get_runs impl_1]] ne "100%"} { fail "implementation failed: see [get_property DIRECTORY [get_runs impl_1]]/runme.log" }

open_run impl_1
set rpt [file join $build reports]
file mkdir $rpt
report_timing_summary -max_paths 10 -report_unconstrained -file [file join $rpt timing_summary.rpt]
report_utilization                -file [file join $rpt utilization.rpt]
report_drc                        -file [file join $rpt drc.rpt]
report_methodology                -file [file join $rpt methodology.rpt]
report_cdc -details               -file [file join $rpt cdc.rpt]
report_io                         -file [file join $rpt io.rpt]

set wns [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
set whs [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -hold]]
puts "Post-route WNS = $wns ns, WHS = $whs ns"
if {$wns eq "" || $wns < 0 || $whs eq "" || $whs < 0} { fail "timing not met (WNS $wns, WHS $whs): see $rpt/timing_summary.rpt" }

write_hw_platform -fixed -include_bit -force -file $xsa
puts "DONE: bitstream + $xsa (Vitis platform). Reports in $rpt"
