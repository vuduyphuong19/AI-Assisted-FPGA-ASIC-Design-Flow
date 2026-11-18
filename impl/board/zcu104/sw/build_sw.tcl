# build_sw.tcl - build the bare-metal demo (uart_apb_test.c) for the ZCU104 Block Design (Vitis 2022.2 XSCT)
#   source /tools/Xilinx/Vitis/2022.2/settings64.sh
#   xsct impl/board/zcu104/sw/build_sw.tcl
# Input : impl/board/zcu104/build/uart_apb_zcu104.xsa (from build_bd.tcl; the bd_only XSA is enough)
# Output: impl/board/zcu104/build/vitis_ws/uart_apb_demo/Debug/uart_apb_demo.elf
#         plus the platform (FSBL, psu_init.tcl) used by run_jtag.tcl

set here  [file normalize [file dirname [info script]]]
set build [file normalize [file join $here .. build]]
set xsa   [file join $build uart_apb_zcu104.xsa]
set ws    [file join $build vitis_ws]
set app   uart_apb_demo

if {![file exists $xsa]} {
    puts "ERROR: $xsa not found: run build_bd.tcl first"
    exit 1
}
file delete -force $ws
file mkdir $ws
setws $ws

# standalone application on A53 core 0; XSCT creates the platform (with FSBL) from the XSA
set created 0
foreach tmpl {{Empty Application(C)} {Empty Application}} {
    if {![catch {app create -name $app -hw $xsa -proc psu_cortexa53_0 -os standalone -lang C -template $tmpl} err]} {
        set created 1
        break
    }
    puts "INFO: template '$tmpl' not available: $err"
}
if {!$created} { puts "ERROR: could not create the application"; exit 1 }

importsources -name $app -path [file join $here uart_apb_test.c]
app build -name $app

set elf [glob -nocomplain [file join $ws $app Debug $app.elf]]
if {$elf eq ""} { puts "ERROR: build produced no ELF (see the Vitis console output above)"; exit 1 }
puts "DONE: $elf"
