`timescale 1ns / 1ps
// bd_stub.sv - DRY-RUN stand-in for the Vivado Block Design, so tb_design_1.sv can be checked
// with Icarus Verilog without Vivado. It is NOT the Xilinx IP: there is no smartconnect,
// axi_apb_bridge or real PS VIP. It keeps the hierarchy names the testbench uses
// (design_1_i.zynq_ultra_ps_e_0.inst, .pl_clk0, uart_0) and a tiny APB master with the VIP task API
// (por_srstb_reset, fpga_soft_reset, write_data, read_data; pslverr -> resp 2'b10 like SLVERR).
// The real rtl/uart_apb.v and uart_apb_bd.v are used.
//   iverilog -g2012 -o bd.vvp -s tb_design_1 rtl/uart_apb.v impl/board/zcu104/uart_apb_bd.v \
//            impl/board/zcu104/sim/tb_design_1.sv impl/board/zcu104/sim/dryrun/bd_stub.sv && vvp -n bd.vvp
// The real BD check is sim_bd.tcl (XSim, Vivado IP + PS VIP).
module design_1_wrapper (input wire rxd, output wire txd);
    design_1 design_1_i (.rxd(rxd), .txd(txd));
endmodule

module design_1 (input wire rxd, output wire txd);
    wire        clk, rstn, psel, penable, pwrite, pready, pslverr, irq;
    wire [31:0] paddr, pwdata, prdata;
    ps_stub zynq_ultra_ps_e_0 (.pl_clk0(clk), .rstn(rstn), .psel(psel), .penable(penable), .pwrite(pwrite),
                               .paddr(paddr), .pwdata(pwdata), .prdata(prdata), .pslverr(pslverr));
    uart_apb_bd uart_0 (.clk(clk), .rst_n(rstn), .s_apb_paddr(paddr), .s_apb_psel(psel),
                        .s_apb_penable(penable), .s_apb_pwrite(pwrite), .s_apb_pwdata(pwdata),
                        .s_apb_prdata(prdata), .s_apb_pready(pready), .s_apb_pslverr(pslverr),
                        .irq(irq), .rxd(rxd), .txd(txd));
endmodule

module ps_stub (output wire pl_clk0, output wire rstn, output wire psel, output wire penable,
                output wire pwrite, output wire [31:0] paddr, output wire [31:0] pwdata,
                input wire [31:0] prdata, input wire pslverr);
    vip_stub inst (.prdata(prdata), .pslverr(pslverr));
    assign pl_clk0 = inst.clk;
    assign rstn    = inst.rstn;
    assign psel    = inst.psel;
    assign penable = inst.penable;
    assign pwrite  = inst.pwrite;
    assign paddr   = inst.paddr;
    assign pwdata  = inst.pwdata;
endmodule

module vip_stub (input wire [31:0] prdata, input wire pslverr);
    reg        clk = 0, rstn = 0, psel = 0, penable = 0, pwrite = 0;
    reg [31:0] paddr = 0, pwdata = 0;
    reg        por = 0, pl = 1;
    reg [4:0]  cnt = 0;
    always #5 clk = ~clk;                                   // 100 MHz, like pl_clk0
    always @(posedge clk) begin                             // proc_sys_reset-like release, 16 cycles
        if (!por || pl) begin cnt <= 0; rstn <= 0; end
        else if (cnt != 5'd16) cnt <= cnt + 1;
        else rstn <= 1;
    end
    task por_srstb_reset(input bit v); por = v; endtask
    task fpga_soft_reset(input [31:0] v); pl = v[0]; endtask
    task write_data(input [39:0] a, input [7:0] sz, input [1023:0] d, output [1:0] resp);
        @(posedge clk); psel <= 1; penable <= 0; pwrite <= 1; paddr <= a[31:0]; pwdata <= d[31:0];
        @(posedge clk); penable <= 1;
        @(posedge clk); resp = pslverr ? 2'b10 : 2'b00; psel <= 0; penable <= 0;
    endtask
    task read_data(input [39:0] a, input [7:0] sz, output [1023:0] d, output [1:0] resp);
        @(posedge clk); psel <= 1; penable <= 0; pwrite <= 0; paddr <= a[31:0];
        @(posedge clk); penable <= 1;
        @(posedge clk); d = {992'b0, prdata}; resp = pslverr ? 2'b10 : 2'b00; psel <= 0; penable <= 0;
    endtask
endmodule
