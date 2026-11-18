// sync_fifo_props: Formal agent output. Properties derived from SPEC-FIFO-1.0 (REQ-3, REQ-4, REQ-5).
// Only assumption: reset is asserted in the first cycle (spec: REQ-1 defines behaviour after reset).
module sync_fifo_props (
  input wire       clk,
  input wire       rst_n,
  input wire       wr_en,
  input wire [7:0] wr_data,
  input wire       rd_en
);
  wire [7:0] rd_data;
  wire       full, empty;
  wire [2:0] count;
  sync_fifo #(.WIDTH(8), .DEPTH(4), .AW(2)) dut (
    .clk(clk), .rst_n(rst_n), .wr_en(wr_en), .wr_data(wr_data),
    .rd_en(rd_en), .rd_data(rd_data), .full(full), .empty(empty), .count(count));

  reg init = 1'b1;
  always @(posedge clk) init <= 1'b0;
  always @(*) if (init) assume (!rst_n);              // ASSUMPTION A1: reset in cycle 0

  always @(posedge clk) if (!init) begin
    assert (count <= 3'd4);                          // REQ-3/REQ-4: no overflow, no underflow wrap
    assert (full  == (count == 3'd4));               // REQ-5
    assert (empty == (count == 3'd0));               // REQ-5
  end
endmodule
