// tb_sync_fifo: DV agent output. Derived from SPEC-FIFO-1.0 requirements only (no RTL notes).
// Reference model + per-requirement checks + functional coverage. On the first mismatch after
// a transaction, the checker attributes the failure to the requirement that transaction
// exercises, then resets DUT and model together so one bug does not contaminate later checks.
`timescale 1ns/1ps
module tb;
  localparam WIDTH = 8;
  localparam DEPTH = 4;
  localparam NCYC  = 3000;

  reg              clk = 1'b0;
  reg              rst_n = 1'b0;
  reg              wr_en = 1'b0;
  reg              rd_en = 1'b0;
  reg  [WIDTH-1:0] wr_data = {WIDTH{1'b0}};
  wire [WIDTH-1:0] rd_data;
  wire             full, empty;
  wire [2:0]       count;

  sync_fifo #(.WIDTH(WIDTH), .DEPTH(DEPTH), .AW(2)) dut (
    .clk(clk), .rst_n(rst_n), .wr_en(wr_en), .wr_data(wr_data),
    .rd_en(rd_en), .rd_data(rd_data), .full(full), .empty(empty), .count(count));

  always #5 clk = ~clk;

  // ---------------- reference model (circular buffer, capacity DEPTH)
  reg [WIDTH-1:0] q [0:DEPTH-1];
  integer head, n;
  // ---------------- bookkeeping
  integer chk [1:6];
  integer err [1:6];
  integer cov_full, cov_wf, cov_re, cov_sim;
  integer seed, cyc, r, tagc, tagd, wbias;
  reg last_wf, last_re, last_sim, last_rst, mismatch, total_fail;

  task fail(input integer req, input [8*48-1:0] what);
    begin
      err[req] = err[req] + 1;
      mismatch = 1'b1;
      if (err[req] <= 3)
        $display("CHECK_FAIL REQ-%0d cyc=%0d %0s: dut(count=%0d full=%b empty=%b rd_data=%h) model(n=%0d head_data=%h)",
                 req, cyc, what, count, full, empty, rd_data, n, (n > 0) ? q[head] : 8'h00);
    end
  endtask

  initial begin
    if (!$value$plusargs("seed=%d", seed)) seed = 1;
    for (r = 1; r <= 6; r = r + 1) begin chk[r] = 0; err[r] = 0; end
    cov_full = 0; cov_wf = 0; cov_re = 0; cov_sim = 0;
    head = 0; n = 0; total_fail = 1'b0;
    last_wf = 0; last_re = 0; last_sim = 0; last_rst = 1; mismatch = 0;
    for (cyc = 0; cyc < NCYC; cyc = cyc + 1) begin
      @(negedge clk);
      // ---------- 1. check DUT against the model (state after the last rising edge)
      if (cyc > 0) begin
        mismatch = 1'b0;
        if (last_rst) begin
          chk[1] = chk[1] + 1;
          if (count !== 3'd0 || full !== 1'b0 || empty !== 1'b1) fail(1, "after reset");
        end else begin
          tagc = last_wf ? 3 : (last_re ? 4 : (last_sim ? 6 : 5));
          tagd = last_wf ? 3 : (last_re ? 4 : (last_sim ? 6 : 2));
          chk[tagc] = chk[tagc] + 1;
          if (count !== n[2:0] || full !== (n == DEPTH) || empty !== (n == 0)) fail(tagc, "count/flags");
          if (n > 0) begin
            chk[tagd] = chk[tagd] + 1;
            if (rd_data !== q[head]) fail(tagd, "rd_data order");
          end
        end
        if (mismatch) total_fail = 1'b1;
      end
      // ---------- 2. choose stimulus (three phases: fill-heavy, drain-heavy, mixed)
      wbias = (cyc < NCYC/3) ? 80 : ((cyc < 2*NCYC/3) ? 20 : 50);
      if (mismatch || cyc < 2 || ($random(seed) % 200 == 0)) begin
        rst_n = 1'b0; wr_en = 1'b0; rd_en = 1'b0;     // resynchronise after a mismatch
      end else begin
        rst_n = 1'b1;
        wr_en = (($random(seed) & 32'h7fffffff) % 100) < wbias;
        rd_en = (($random(seed) & 32'h7fffffff) % 100) < (100 - wbias);
        wr_data = $random(seed);
      end
      // ---------- 3. predict the state after the next rising edge
      last_rst = !rst_n;
      last_wf  = rst_n && wr_en && (n == DEPTH);
      last_re  = rst_n && rd_en && (n == 0);
      last_sim = rst_n && wr_en && rd_en && (n > 0) && (n < DEPTH);
      if (last_wf)  cov_wf = cov_wf + 1;
      if (last_re)  cov_re = cov_re + 1;
      if (last_sim) cov_sim = cov_sim + 1;
      if (!rst_n) begin
        head = 0; n = 0;
      end else begin
        if (wr_en && n < DEPTH) begin
          q[(head + n) % DEPTH] = wr_data;
          n = n + 1;
          if (rd_en && n > 1) begin head = (head + 1) % DEPTH; n = n - 1; end
        end else if (rd_en && n > 0) begin
          head = (head + 1) % DEPTH; n = n - 1;
        end
      end
      if (n == DEPTH) cov_full = cov_full + 1;
    end
    for (r = 1; r <= 6; r = r + 1)
      $display("REQ_RESULT REQ-%0d %0s checks=%0d errors=%0d", r,
               (err[r] == 0 && chk[r] > 0) ? "PASS" : "FAIL", chk[r], err[r]);
    $display("COVER full_reached %0d", cov_full);
    $display("COVER write_when_full %0d", cov_wf);
    $display("COVER read_when_empty %0d", cov_re);
    $display("COVER simultaneous_rw %0d", cov_sim);
    $display("SUMMARY %0s", total_fail ? "FAIL" : "PASS");
    $finish;
  end
endmodule
