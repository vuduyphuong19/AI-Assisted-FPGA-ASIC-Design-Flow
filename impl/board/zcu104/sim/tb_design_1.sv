`timescale 1ns / 1ps
// tb_design_1.sv - Block Design level testbench for uart_apb on ZCU104 (Vivado XSim).
//
// Verifies the integration that the block-level flow could not see: the PS simulation model
// (Zynq UltraScale+ VIP) does real AXI transfers on M_AXI_HPM0_FPD -> smartconnect ->
// axi_apb_bridge -> uart_apb. Checks:
//   1. reset values through the bus (STATUS 0x0A, BAUD 868, CTRL 0)          REQ-1, REQ-3
//   2. pslverr reaches the AXI master as SLVERR (RXDATA empty, unmapped, BAUD < 4)   REQ-4/16/22
//   3. register write/readback at 0xA000_0000 (address map)                   REQ-3
//   4. txd -> rxd loopback of 4 bytes, decoded independently on txd           REQ-8/9/13/15/16
//   5. irq (IE_RXNE) follows RX_EMPTY                                          REQ-23
//   6. external rxd frame with a stop bit 0 -> FRAME_ERR, W1C clear, good frame  REQ-18/20
// Result line: TB_RESULT PASS|FAIL checks=<n> errors=<n>   (sim_bd.tcl greps it)
//
// Run: vivado -mode batch -source impl/board/zcu104/sim_bd.tcl
module tb_design_1;

    localparam logic [39:0] BASE   = 40'h00_A000_0000;
    localparam logic [7:0]  R_TX   = 8'h00, R_RX = 8'h04, R_ST = 8'h08, R_CTRL = 8'h0C, R_BAUD = 8'h10,
                            R_NONE = 8'h14;
    localparam logic [1:0]  OKAY   = 2'b00, SLVERR = 2'b10;
    localparam int          D_SIM  = 16;                 // BAUD used in simulation (cycles per bit)

    localparam logic [31:0] ST_TX_FULL = 32'h01, ST_TX_EMPTY = 32'h02, ST_RX_EMPTY = 32'h08,
                            ST_FRAME_ERR = 32'h40;

    // ------------------------------------------------------------------ DUT: the whole Block Design
    wire  txd;
    logic loop_en = 1'b1;                                // 1: rxd = txd (loopback), 0: tb drives rxd
    logic rxd_drv = 1'b1;
    wire  rxd = loop_en ? txd : rxd_drv;

    design_1_wrapper dut (.rxd(rxd), .txd(txd));

    `define PS dut.design_1_i.zynq_ultra_ps_e_0.inst
    wire clk   = dut.design_1_i.zynq_ultra_ps_e_0.pl_clk0;
    wire rst_n = dut.design_1_i.uart_0.rst_n;
    wire irq   = dut.design_1_i.uart_0.irq;

    int         checks = 0, errors = 0;
    realtime    bit_ns = 0.0;
    logic [7:0] bytes [4];                                    // loopback payload, set in main

    // ------------------------------------------------------------------ helpers
    task automatic check(input string what, input logic [31:0] got, input logic [31:0] exp);
        checks++;
        if (got !== exp) begin
            errors++;
            $display("TB_CHECK_FAIL %s: got 0x%08h expected 0x%08h at %0t ns", what, got, exp, $time);
        end
    endtask

    task automatic wr(input logic [7:0] off, input logic [31:0] data, input logic [1:0] exp_resp = OKAY);
        logic [1:0] resp;
        `PS.write_data(BASE + off, 8'd4, data, resp);
        check($sformatf("write +0x%02h resp", off), resp, exp_resp);
    endtask

    task automatic rd(input logic [7:0] off, output logic [31:0] data, input logic [1:0] exp_resp = OKAY);
        logic [1:0]    resp;
        logic [1023:0] rdata;
        `PS.read_data(BASE + off, 8'd4, rdata, resp);
        data = rdata[31:0];
        check($sformatf("read +0x%02h resp", off), resp, exp_resp);
    endtask

    task automatic expect_reg(input logic [7:0] off, input logic [31:0] exp, input string what);
        logic [31:0] v;
        rd(off, v);
        check(what, v, exp);
    endtask

    // poll STATUS until (STATUS & mask) == val, at most max_polls reads
    task automatic wait_status(input logic [31:0] mask, input logic [31:0] val, input int max_polls,
                               input string what);
        logic [31:0] st;
        int n = 0;
        do begin
            rd(R_ST, st);
            n++;
        end while (((st & mask) != val) && n < max_polls);
        check(what, st & mask, val);
    endtask

    // drive one 8N1 frame on rxd (tb side), stop bit value selectable
    task automatic send_frame(input logic [7:0] d, input logic stop);
        rxd_drv = 1'b0; #(bit_ns);
        for (int i = 0; i < 8; i++) begin rxd_drv = d[i]; #(bit_ns); end
        rxd_drv = stop; #(bit_ns);
        rxd_drv = 1'b1; #(2 * bit_ns);
    endtask

    // ------------------------------------------------------------------ independent txd decoder
    logic [7:0] mon_q [$];
    initial begin : txd_monitor
        logic [7:0] b;
        wait (rst_n === 1'b1 && bit_ns > 0.0);
        forever begin
            @(negedge txd);
            #(1.5 * bit_ns);                             // middle of data bit 0
            for (int i = 0; i < 8; i++) begin b[i] = txd; #(bit_ns); end
            checks++;
            if (txd !== 1'b1) begin errors++; $display("TB_CHECK_FAIL txd stop bit is %b at %0t ns", txd, $time); end
            mon_q.push_back(b);
        end
    end

    // ------------------------------------------------------------------ watchdog
    initial begin
        #5ms;
        $display("TB_CHECK_FAIL watchdog: simulation did not finish in 5 ms");
        $display("TB_RESULT FAIL checks=%0d errors=%0d", checks, errors + 1);
        $finish;
    end

    // ------------------------------------------------------------------ test sequence
    initial begin : main
        logic [31:0] v;
        realtime     t0;

        bytes[0] = 8'h55; bytes[1] = 8'hA3; bytes[2] = 8'h00; bytes[3] = 8'hFF;

        // PS power-on reset and PL reset (VIP API)
        `PS.por_srstb_reset(1'b0);
        `PS.fpga_soft_reset(32'h1);
        #200;
        `PS.por_srstb_reset(1'b1);
        `PS.fpga_soft_reset(32'h0);

        // measure pl_clk0 and wait for the synchronous reset from proc_sys_reset to release
        @(posedge clk); t0 = $realtime; @(posedge clk);
        bit_ns = D_SIM * ($realtime - t0);
        $display("TB_INFO pl_clk0 period %0.3f ns, bit time %0.1f ns at BAUD=%0d", $realtime - t0, bit_ns, D_SIM);
        fork
            wait (rst_n === 1'b1);
            begin #100us; end
        join_any
        disable fork;
        check("PL reset released", rst_n, 1'b1);
        repeat (10) @(posedge clk);

        // 1. reset values through AXI -> APB
        expect_reg(R_ST,   32'h0000_000A, "STATUS after reset");
        expect_reg(R_BAUD, 32'd868,       "BAUD after reset");
        expect_reg(R_CTRL, 32'h0,         "CTRL after reset");

        // 2. spec error responses arrive as AXI SLVERR
        rd(R_RX, v, SLVERR);   check("RXDATA empty reads 0", v, 32'h0);
        rd(R_NONE, v, SLVERR); check("unmapped reads 0", v, 32'h0);
        wr(R_BAUD, 32'd2, SLVERR);
        expect_reg(R_BAUD, 32'd868, "BAUD unchanged by value < 4");

        // 3. configuration: BAUD 16, TX_EN | RX_EN | IE_RXNE
        wr(R_BAUD, D_SIM);
        expect_reg(R_BAUD, D_SIM, "BAUD readback");
        wr(R_CTRL, 32'h13);
        expect_reg(R_CTRL, 32'h13, "CTRL readback");
        repeat (3) @(posedge clk);
        check("irq low with RX empty", irq, 1'b0);

        // 4. loopback txd -> rxd
        foreach (bytes[i]) begin
            wait_status(ST_TX_FULL, 32'h0, 200, "TX not full before write");
            wr(R_TX, bytes[i]);
        end
        foreach (bytes[i]) begin
            wait_status(ST_RX_EMPTY, 32'h0, 2000, $sformatf("byte %0d received", i));
            repeat (2) @(posedge clk);
            check("irq high with RX not empty (IE_RXNE)", irq, 1'b1);
            rd(R_RX, v);
            check($sformatf("RXDATA byte %0d", i), v, {24'h0, bytes[i]});
        end
        wait_status(ST_TX_EMPTY | ST_RX_EMPTY, ST_TX_EMPTY | ST_RX_EMPTY, 2000, "TX and RX FIFOs empty");
        #(2 * bit_ns);
        check("irq low after RX drained", irq, 1'b0);
        check("txd monitor frame count", mon_q.size(), 4);
        foreach (bytes[i]) if (i < mon_q.size()) check($sformatf("txd decoded byte %0d", i), mon_q[i], bytes[i]);

        // 5. external line: framing error, W1C clear, then a good frame
        loop_en = 1'b0;
        #(2 * bit_ns);
        send_frame(8'h3C, 1'b0);
        expect_reg(R_ST, 32'h0000_004A, "STATUS: FRAME_ERR set, byte dropped");
        wr(R_ST, ST_FRAME_ERR);
        expect_reg(R_ST, 32'h0000_000A, "STATUS: FRAME_ERR cleared by W1C");
        send_frame(8'hC5, 1'b1);
        wait_status(ST_RX_EMPTY, 32'h0, 200, "external byte received");
        expect_reg(R_RX, 32'h0000_00C5, "RXDATA external byte");

        $display("TB_RESULT %s checks=%0d errors=%0d", errors == 0 ? "PASS" : "FAIL", checks, errors);
        $finish;
    end

endmodule
