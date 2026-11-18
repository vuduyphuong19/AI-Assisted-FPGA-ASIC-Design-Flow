`timescale 1ns/1ps
// ============================================================================
// tb_uart_apb : self-checking testbench for uart_apb (SPEC-UART-APB-1.0)
// Derived from spec/requirements.json only (no RTL knowledge).
//
// Method
//  * Inputs are driven at negedge; every output is checked 4 ns later (1 ns
//    before the next posedge), i.e. once per clk cycle, against a reference
//    model written from the requirement text.  The model is then advanced to
//    the state after the next posedge using the same inputs the DUT samples.
//  * Where the spec allows a latency range (TX start <= 3 cycles, TX_BUSY
//    window, RX store <= 6 cycles after the nominal stop end, irq lag <= 1)
//    the model keeps both legal values ("windows") and accepts either, and
//    flags any value outside the window.
//  * rxd is driven at arbitrary sub-cycle phase (asynchronous input).
// Output contract: REQ_RESULT / COVER / CHECK_FAIL / SUMMARY (see report).
// ============================================================================
module tb_uart_apb;

  localparam integer FIFO_DEPTH = 4;
  localparam integer ADDR_W     = 5;
  localparam integer DIV_RESET  = 868;
  localparam integer DIV_MIN    = 4;
  localparam integer CLKP       = 10;      // 100 MHz
  localparam integer NREQ       = 23;
  localparam integer NBIN       = 30;      // 25 spec bins + extra bins
  localparam integer MAXFAIL    = 40;
  localparam integer CAL_PHASE  = 3300;    // ps, fixed rxd phase for calibrated scenarios

  localparam [4:0] A_TX = 5'h00, A_RX = 5'h04, A_ST = 5'h08, A_CTRL = 5'h0C, A_BAUD = 5'h10;

  // coverage bin indices (spec bins 0..24, extra 25..)
  localparam integer B_TX_FULL = 0,  B_TX_WR_FULL = 1, B_TX_B2B = 2,  B_TX_DIS = 3;
  localparam integer B_RX_FULL = 4,  B_RX_RD_EMPTY = 5, B_RX_B2B = 6, B_RX_OVR = 7;
  localparam integer B_RX_OVR_RD = 8, B_RX_FERR = 9,  B_RX_PERR = 10, B_RX_FALSE = 11;
  localparam integer B_RX_MISM = 12, B_RX_DIS = 13,  B_PAR_EVEN = 14, B_PAR_ODD = 15;
  localparam integer B_BAUD_CHG = 16, B_BAUD_LOW = 17, B_INVALID = 18, B_W1C = 19;
  localparam integer B_SAME_EDGE = 20, B_IRQ_RXNE = 21, B_IRQ_TXE = 22, B_IRQ_ERR = 23;
  localparam integer B_RESET_MID = 24;
  localparam integer X_APB_WR = 25, X_APB_RD = 26, X_TX_POP_RACE = 27, X_TX_FRAMES = 28, X_RX_FRAMES = 29;

  // --------------------------------------------------------------------------
  // clock, signals, DUT
  // --------------------------------------------------------------------------
  reg clk = 1'b0;
  always #(CLKP/2) clk = ~clk;

  integer cyc = 0;
  always @(posedge clk) cyc <= cyc + 1;

  reg               rst_n;
  reg               psel, penable, pwrite;
  reg  [ADDR_W-1:0] paddr;
  reg  [31:0]       pwdata;
  reg               rxd;
  wire [31:0]       prdata;
  wire              pready, pslverr, txd, irq;

  uart_apb dut (
    .clk(clk), .rst_n(rst_n),
    .psel(psel), .penable(penable), .pwrite(pwrite), .paddr(paddr), .pwdata(pwdata),
    .prdata(prdata), .pready(pready), .pslverr(pslverr),
    .rxd(rxd), .txd(txd), .irq(irq)
  );

  integer seed;
  function integer rnd(input integer n);
    begin
      if (n <= 1) rnd = 0;
      else rnd = ($random(seed) & 32'h7fffffff) % n;
    end
  endfunction

  // --------------------------------------------------------------------------
  // check bookkeeping
  // --------------------------------------------------------------------------
  integer req_chk [0:NREQ];
  integer req_err [0:NREQ];
  integer cov     [0:NBIN-1];
  integer nfail;
  integer tag_req;        // scenario attribution: checks also count for this REQ
  reg     watchdog_hit;

  task ckr(input integer req, input [8*20:1] sig, input ok, input [31:0] e, input [31:0] a,
           input [8*44:1] ctx);
    begin
      req_chk[req] = req_chk[req] + 1;
      if (ok !== 1'b1) req_err[req] = req_err[req] + 1;
      if (tag_req > 0 && tag_req != req) begin
        req_chk[tag_req] = req_chk[tag_req] + 1;
        if (ok !== 1'b1) req_err[tag_req] = req_err[tag_req] + 1;
      end
      if (ok !== 1'b1) begin
        nfail = nfail + 1;
        if (nfail <= MAXFAIL)
          $display("CHECK_FAIL cycle=%0d REQ-%0d %0s exp=%h act=%h %0s (tag REQ-%0d)",
                   cyc, req, sig, e, a, ctx, tag_req);
      end
    end
  endtask

  task ck(input integer req, input [8*20:1] sig, input [31:0] e, input [31:0] a, input [8*44:1] ctx);
    ckr(req, sig, (a === e), e, a, ctx);
  endtask

  // --------------------------------------------------------------------------
  // reference model state (value visible in the current cycle)
  // --------------------------------------------------------------------------
  reg         model_valid;
  reg         reset_next;
  reg  [6:0]  m_ctrl;
  reg  [15:0] m_baud;
  // TX FIFO model: queue of bytes not yet started on txd
  reg  [7:0]  txq [0:7];
  integer     tx_qlen;
  reg         tx_popped;      // DUT observed to have removed the head before its start bit
  reg         tx_inframe;
  integer     tx_S, tx_L, tx_D, tx_nbits;
  reg  [10:0] tx_bits;
  reg  [7:0]  tx_byte;
  reg  [1:0]  tx_par;         // {odd, en}
  integer     tx_R;           // REQ-10 reference cycle
  integer     tx_lastL;       // last cycle of the previous stop bit
  integer     busy_pre;       // cycle of an unexplained TX_BUSY=1 (must be <=3 before a start)
  reg         tx_late_rep;
  integer     tx_prevD;
  // RX FIFO model
  reg  [7:0]  rxq [0:7];
  integer     rxq_tag [0:7];
  integer     rx_cnt;
  reg  [2:0]  m_err;          // {PARITY_ERR, FRAME_ERR, RX_OVERRUN} = STATUS[7:5]
  reg         w1c_recent;
  integer     last_commit_c;
  integer     rx_prevD;
  // pending RX frame outcomes (created by the rxd driver at the falling edge)
  integer     ev_n;
  integer     ev_kind  [0:3];  // 0 = valid, bit0 = framing error, bit1 = parity error
  reg  [7:0]  ev_byte  [0:3];
  integer     ev_open  [0:3];  // first cycle in which the DUT may show the outcome
  integer     ev_commit[0:3];  // model applies outcome at the edge ending this cycle
  integer     ev_tag   [0:3];
  integer     ev_D     [0:3];
  integer     ev_par   [0:3];
  integer     ev_jit   [0:3];
  reg  [1:0]  irq_pposs;       // irq values allowed by the previous cycle's state

  // TB-side flags shared by stimulus threads
  reg         rx_line_busy;
  reg         rx_abort;
  reg         rx_inflight;
  reg         cfg_lock;
  reg         stress_on;
  integer     cal_rel;
  integer     last_cf;
  integer     cur_jit;
  real        rx_end_rt;
  reg         rx_prev_ok;

  function irqf(input [6:0] ct, input integer txc, input integer rxc, input [2:0] er);
    irqf = (ct[4] && (rxc != 0)) || (ct[5] && (txc == 0)) || (ct[6] && (er != 3'b000));
  endfunction

  task model_reset;
    begin
      m_ctrl = 7'h00; m_baud = DIV_RESET;
      tx_qlen = 0; tx_popped = 1'b0; tx_inframe = 1'b0;
      tx_R = -100; tx_lastL = -100; busy_pre = -1; tx_late_rep = 1'b0; tx_prevD = 0;
      rx_cnt = 0; m_err = 3'b000; w1c_recent = 1'b0; rx_prevD = 0;
      ev_n = 0; irq_pposs = 2'b01;
    end
  endtask

  task rx_make_event(input integer kind, input [7:0] b, input integer open, input integer commit,
                     input integer tag, input integer D, input integer par, input integer jit);
    begin
      if (ev_n < 4) begin
        ev_kind[ev_n] = kind; ev_byte[ev_n] = b; ev_open[ev_n] = open; ev_commit[ev_n] = commit;
        ev_tag[ev_n] = tag; ev_D[ev_n] = D; ev_par[ev_n] = par; ev_jit[ev_n] = jit;
        ev_n = ev_n + 1;
      end else
        $display("NOTE tb: RX event queue overflow at cycle %0d", cyc);
    end
  endtask

  task ev_drop(input integer idx);
    integer q;
    begin
      for (q = idx; q < 3; q = q + 1) begin
        ev_kind[q] = ev_kind[q+1]; ev_byte[q] = ev_byte[q+1]; ev_open[q] = ev_open[q+1];
        ev_commit[q] = ev_commit[q+1]; ev_tag[q] = ev_tag[q+1]; ev_D[q] = ev_D[q+1];
        ev_par[q] = ev_par[q+1]; ev_jit[q] = ev_jit[q+1];
      end
      ev_n = ev_n - 1;
    end
  endtask

  // --------------------------------------------------------------------------
  // per-cycle checker + model (runs at negedge + 4 ns)
  // --------------------------------------------------------------------------
  integer c, k, pos, i, lo, hi, tc, paidx, pre_cnt;
  reg     expb, acc, rd_a, wr_a, pw, in_frame_c, tx_end_now, start_now, rx_win;
  reg     commit, commit_now, pop, okv, sel_post, exp_full, push_tx, store_ok;
  reg [2:0]  w1c, set_bits, post_err;
  integer    post_cnt;
  reg [4:0]  pre_g, post_g, act_g, exp_g;   // {PERR, FERR, OVR, RX_EMPTY, RX_FULL}
  reg [31:0] act;
  reg [1:0]  poss;
  reg [6:0]  n_ctrl;
  reg [15:0] n_baud;
  reg [7:0]  push_byte, store_byte;
  integer    store_tag;

  task status_check;
    begin
      act = prdata;
      ck(3, "pslverr", 0, pslverr, "read STATUS");
      ck(5, "STATUS[31:8]", 0, {8'h00, act[31:8]}, "reserved STATUS bits read 0");
      // TX_FULL / TX_EMPTY: any FIFO count the window allows
      okv = 1'b0;
      for (tc = lo; tc <= hi; tc = tc + 1)
        if (act[1:0] === {(tc == 0), (tc == FIFO_DEPTH)}) okv = 1'b1;
      ckr(7, "STATUS.TX_E/F", okv, {30'h0, (hi == 0), (hi == FIFO_DEPTH)}, {30'h0, act[1:0]},
          "TX_EMPTY/TX_FULL vs TX FIFO count");
      if (okv && !tx_popped && (lo != hi) &&
          (act[1:0] === {(lo == 0), (lo == FIFO_DEPTH)}) &&
          ({(lo == 0), (lo == FIFO_DEPTH)} != {(hi == 0), (hi == FIFO_DEPTH)}))
        tx_popped = 1'b1;
      // RX flags and error bits: pre or post value of the pending frame outcome
      act_g = {act[7], act[6], act[5], act[3], act[2]};
      sel_post = rx_win && (act_g === post_g) && (act_g !== pre_g);
      exp_g = sel_post ? post_g : pre_g;
      ck(7,  "STATUS.RX_E/F", {30'h0, exp_g[1:0]}, {30'h0, act_g[1:0]}, "RX_EMPTY/RX_FULL vs RX FIFO count");
      if (c - last_commit_c <= 4)
        ck(15, "STATUS.RX_E/F", {30'h0, exp_g[1:0]}, {30'h0, act_g[1:0]}, "RX flags right after store deadline");
      ck(17, "STATUS.RX_OVERRUN", exp_g[2], act_g[2], "sticky overrun bit");
      ck(18, "STATUS.FRAME_ERR",  exp_g[3], act_g[3], "sticky framing error bit");
      ck(19, "STATUS.PARITY_ERR", exp_g[4], act_g[4], "sticky parity error bit");
      if (w1c_recent) begin
        ck(20, "STATUS[7:5]", {29'h0, exp_g[4:2]}, {29'h0, act_g[4:2]}, "error bits after W1C write");
        w1c_recent = 1'b0;
      end
      // TX_BUSY (REQ-12)
      if (in_frame_c)
        ck(12, "STATUS.TX_BUSY", 1, act[4], "txd carries a frame");
      else if (tx_lastL >= 0 && c == tx_lastL + 1)
        ckr(12, "STATUS.TX_BUSY", (act[4] === 1'b0) || (act[4] === 1'b1), 0, act[4], "1 cycle after stop (either)");
      else if (act[4] === 1'b1) begin
        if (busy_pre < 0) busy_pre = c;     // must be followed by a start bit within 3 cycles
      end else
        ck(12, "STATUS.TX_BUSY", 0, act[4], "no frame on txd");
    end
  endtask

  task cycle_check;
    begin
      if (reset_next) begin
        ck(1, "txd",     1, txd,     "cycle after reset edge");
        ck(1, "irq",     0, irq,     "cycle after reset edge");
        ck(1, "pslverr", 0, pslverr, "cycle after reset edge");
        ck(1, "prdata",  0, prdata,  "cycle after reset edge");
        reset_next = 1'b0;
      end
      // ---------------- APB protocol rules (REQ-2) ----------------
      ck(2, "pready", 1, pready, "pready constant 1");
      acc = (psel === 1'b1) && (penable === 1'b1);
      rd_a = acc && (pwrite === 1'b0);
      wr_a = acc && (pwrite === 1'b1);
      if (!acc)  ck(2, "pslverr", 0, pslverr, "not an access cycle");
      if (!rd_a) ck(2, "prdata",  0, prdata,  "not a read access cycle");
      if (wr_a) cov[X_APB_WR] = cov[X_APB_WR] + 1;
      if (rd_a) cov[X_APB_RD] = cov[X_APB_RD] + 1;

      // ---------------- TX monitor (REQ-8..12) ----------------
      start_now = 1'b0; tx_end_now = 1'b0; in_frame_c = 1'b0;
      if (tx_inframe) begin
        in_frame_c = 1'b1;
        k = (c - tx_S) / tx_D; pos = (c - tx_S) % tx_D;
        expb = tx_bits[k];
        ckr(((pos == 0) || (pos == tx_D - 1)) ? 9 : 8, "txd", (txd === expb), expb, txd,
            "frame bit value/length");
        if (c == tx_L) tx_end_now = 1'b1;
      end else if (txd === 1'b0) begin
        start_now = 1'b1; in_frame_c = 1'b1;
        ck(11, "TX_EN@start", 1, m_ctrl[0], "start bit while TX_EN=0");
        ckr(10, "start_latency", (c > tx_R) && (c - tx_R <= 4), tx_R + 4, c,
            "start bit cycle must be in (R, R+4]");
        ckr(8, "tx_fifo@start", (tx_qlen > 0), 1, tx_qlen, "frame started with empty TX FIFO");
        if (tx_qlen > 0) begin
          tx_byte = txq[0];
          for (i = 0; i < 7; i = i + 1) txq[i] = txq[i+1];
          tx_qlen = tx_qlen - 1;
        end else tx_byte = 8'h00;
        tx_popped = 1'b0;
        tx_S = c; tx_D = m_baud; tx_par = m_ctrl[3:2];
        tx_nbits = m_ctrl[2] ? 11 : 10;
        tx_bits = 11'h7FF;
        tx_bits[0] = 1'b0;
        tx_bits[8:1] = tx_byte;
        if (m_ctrl[2]) tx_bits[9] = m_ctrl[3] ? ~(^tx_byte) : (^tx_byte);
        tx_L = c + tx_nbits * tx_D - 1;
        tx_inframe = 1'b1;
        if (busy_pre >= 0) begin
          ckr(12, "TX_BUSY_early", (c - busy_pre <= 3), busy_pre + 3, c, "TX_BUSY=1 more than 3 cycles before start");
          busy_pre = -1;
        end
        if (tx_lastL >= 0 && (c - tx_lastL <= 4)) cov[B_TX_B2B] = cov[B_TX_B2B] + 1;
        if (tx_prevD > 0 && tx_D != tx_prevD) cov[B_BAUD_CHG] = cov[B_BAUD_CHG] + 1;
        tx_prevD = tx_D;
        tx_late_rep = 1'b0;
      end else if (txd === 1'b1) begin
        ck(m_ctrl[0] ? 10 : 11, "txd", 1, txd, "idle line between frames");
        if (m_ctrl[0] && tx_qlen > 0 && (c >= tx_R + 4) && !tx_late_rep) begin
          ckr(10, "start_latency", 1'b0, tx_R + 4, c, "no start bit within 3 cycles of trigger");
          tx_late_rep = 1'b1;
        end
      end else
        ck(10, "txd", 1, txd, "txd unknown");
      if (busy_pre >= 0 && (c - busy_pre > 3)) begin
        ckr(12, "TX_BUSY_early", 1'b0, 0, busy_pre, "TX_BUSY=1 not followed by a start bit");
        busy_pre = -1;
      end

      // TX FIFO counts the DUT may hold this cycle
      pw = m_ctrl[0] && (tx_qlen > 0) && (!tx_inframe || (c >= tx_L - tx_D + 1));
      if (tx_popped)  begin lo = tx_qlen - 1; hi = tx_qlen - 1; end
      else if (pw)    begin lo = tx_qlen - 1; hi = tx_qlen; end
      else            begin lo = tx_qlen;     hi = tx_qlen; end
      if (hi > FIFO_DEPTH) hi = FIFO_DEPTH;
      if (lo > FIFO_DEPTH) lo = FIFO_DEPTH;
      if (lo < 0) lo = 0;

      // RX pending outcome window
      rx_win = (ev_n > 0) && (c >= ev_open[0]) && (c <= ev_commit[0]);
      pre_g = {m_err, (rx_cnt == 0), (rx_cnt == FIFO_DEPTH)};
      post_cnt = rx_cnt; post_err = m_err;
      if (ev_n > 0) begin
        if (ev_kind[0] == 0) begin
          if (rx_cnt < FIFO_DEPTH) post_cnt = rx_cnt + 1; else post_err[0] = 1'b1;
        end else begin
          if (ev_kind[0] & 1) post_err[1] = 1'b1;
          if (ev_kind[0] & 2) post_err[2] = 1'b1;
        end
      end
      post_g = {post_err, (post_cnt == 0), (post_cnt == FIFO_DEPTH)};

      // ---------------- APB access ----------------
      pop = 1'b0; w1c = 3'b000; push_tx = 1'b0; commit_now = 1'b0;
      n_ctrl = m_ctrl; n_baud = m_baud; push_byte = pwdata[7:0];
      paidx = paddr[4:2];
      if (rd_a) begin
        case (paidx)
          0: begin
               ck(3, "prdata",  0, prdata,  "read TXDATA returns 0");
               ck(3, "pslverr", 0, pslverr, "read TXDATA");
             end
          1: begin
               if (rx_cnt > 0) begin
                 ck(16, "prdata", {24'h0, rxq[0]}, prdata, "read RXDATA oldest byte");
                 ck(15, "prdata", {24'h0, rxq[0]}, prdata, "stored byte");
                 if (rxq_tag[0] != 16 && rxq_tag[0] != 15)
                   ck(rxq_tag[0], "rx_byte", {24'h0, rxq[0]}, prdata, "byte carried by rxd frame");
                 ck(16, "pslverr", 0, pslverr, "read RXDATA non-empty");
                 pop = 1'b1;
               end else if (rx_win && ev_kind[0] == 0) begin
                 if (pslverr === 1'b0) begin
                   ck(16, "prdata", {24'h0, ev_byte[0]}, prdata, "read RXDATA in store window");
                   commit_now = 1'b1; pop = 1'b1;
                 end else
                   ck(16, "prdata", 0, prdata, "read RXDATA in store window (empty)");
               end else begin
                 ck(16, "prdata",  0, prdata,  "read RXDATA when empty");
                 ck(16, "pslverr", 1, pslverr, "read RXDATA when empty");
                 cov[B_RX_RD_EMPTY] = cov[B_RX_RD_EMPTY] + 1;
               end
             end
          2: status_check;
          3: begin
               ck(3, "prdata",  {25'h0, m_ctrl}, prdata, "read CTRL");
               ck(3, "pslverr", 0, pslverr, "read CTRL");
             end
          4: begin
               ck(22, "prdata",  {16'h0, m_baud}, prdata, "read BAUD");
               ck(3,  "pslverr", 0, pslverr, "read BAUD");
             end
          default: begin
               ck(4, "prdata",  0, prdata,  "read unmapped address");
               ck(4, "pslverr", 1, pslverr, "read unmapped address");
               cov[B_INVALID] = cov[B_INVALID] + 1;
             end
        endcase
      end
      if (wr_a) begin
        case (paidx)
          0: begin
               exp_full = tx_popped ? ((tx_qlen - 1) >= FIFO_DEPTH) : (tx_qlen >= FIFO_DEPTH);
               if (!tx_popped && pw && tx_qlen == FIFO_DEPTH) begin
                 // head may already have left the FIFO: either response is legal
                 ckr(6, "pslverr", (pslverr === 1'b0) || (pslverr === 1'b1), 1, pslverr,
                     "write TXDATA full while pop pending");
                 cov[X_TX_POP_RACE] = cov[X_TX_POP_RACE] + 1;
                 if (pslverr === 1'b0) begin tx_popped = 1'b1; push_tx = 1'b1; end
               end else begin
                 ck(6, "pslverr", exp_full, pslverr, "write TXDATA (1 iff TX_FULL)");
                 if (!exp_full) push_tx = 1'b1;
                 else cov[B_TX_WR_FULL] = cov[B_TX_WR_FULL] + 1;
               end
             end
          1: ck(3, "pslverr", 0, pslverr, "write RXDATA ignored");
          2: begin ck(3, "pslverr", 0, pslverr, "write STATUS"); w1c = pwdata[7:5]; end
          3: begin ck(3, "pslverr", 0, pslverr, "write CTRL"); n_ctrl = pwdata[6:0]; end
          4: begin
               if (pwdata[15:0] >= DIV_MIN) begin
                 ck(22, "pslverr", 0, pslverr, "write BAUD >= DIV_MIN");
                 n_baud = pwdata[15:0];
               end else begin
                 ck(22, "pslverr", 1, pslverr, "write BAUD < DIV_MIN");
                 cov[B_BAUD_LOW] = cov[B_BAUD_LOW] + 1;
               end
             end
          default: begin
               ck(4, "pslverr", 1, pslverr, "write unmapped address");
               cov[B_INVALID] = cov[B_INVALID] + 1;
             end
        endcase
      end

      // ---------------- irq (REQ-23) ----------------
      poss = 2'b00;
      for (tc = lo; tc <= hi; tc = tc + 1) begin
        poss[irqf(m_ctrl, tc, rx_cnt, m_err)] = 1'b1;
        if (rx_win) poss[irqf(m_ctrl, tc, post_cnt, post_err)] = 1'b1;
      end
      ckr(23, "irq", ((irq === 1'b0) && (poss[0] || irq_pposs[0])) || ((irq === 1'b1) && (poss[1] || irq_pposs[1])),
          {30'h0, poss}, irq, "irq vs f(CTRL,STATUS) now or 1 cycle ago");
      irq_pposs = poss;
      if (irq === 1'b1) begin
        if (m_ctrl[4] && rx_cnt > 0 && !rx_win) cov[B_IRQ_RXNE] = cov[B_IRQ_RXNE] + 1;
        if (m_ctrl[5] && hi == 0)               cov[B_IRQ_TXE]  = cov[B_IRQ_TXE] + 1;
        if (m_ctrl[6] && m_err != 0 && !rx_win) cov[B_IRQ_ERR]  = cov[B_IRQ_ERR] + 1;
      end

      // ---------------- next state ----------------
      // RX outcome commit (decision uses the value before this edge)
      commit = (ev_n > 0) && ((c >= ev_commit[0]) || commit_now);
      set_bits = 3'b000; store_ok = 1'b0;
      if (commit) begin
        cov[X_RX_FRAMES] = cov[X_RX_FRAMES] + 1;
        if (ev_kind[0] == 0) begin
          if (rx_cnt < FIFO_DEPTH || commit_now) store_ok = 1'b1;
          else begin
            set_bits[0] = 1'b1;
            cov[B_RX_OVR] = cov[B_RX_OVR] + 1;
            if (pop) cov[B_RX_OVR_RD] = cov[B_RX_OVR_RD] + 1;
          end
          if (ev_par[0] == 1) cov[B_PAR_EVEN] = cov[B_PAR_EVEN] + 1;
          if (ev_par[0] == 3) cov[B_PAR_ODD]  = cov[B_PAR_ODD] + 1;
          if (ev_jit[0] != 0) cov[B_RX_MISM]  = cov[B_RX_MISM] + 1;
          if (rx_prevD > 0 && ev_D[0] != rx_prevD) cov[B_BAUD_CHG] = cov[B_BAUD_CHG] + 1;
          rx_prevD = ev_D[0];
        end else begin
          if (ev_kind[0] & 1) begin set_bits[1] = 1'b1; cov[B_RX_FERR] = cov[B_RX_FERR] + 1; end
          if (ev_kind[0] & 2) begin set_bits[2] = 1'b1; cov[B_RX_PERR] = cov[B_RX_PERR] + 1; end
        end
        store_byte = ev_byte[0]; store_tag = ev_tag[0];
        last_commit_c = c;
        ev_drop(0);
      end
      if (commit_now) begin store_ok = 1'b0; pop = 1'b0; end  // stored earlier, popped by this read
      if (pop) begin
        for (i = 0; i < 7; i = i + 1) begin rxq[i] = rxq[i+1]; rxq_tag[i] = rxq_tag[i+1]; end
        rx_cnt = rx_cnt - 1;
      end
      if (store_ok) begin
        rxq[rx_cnt] = store_byte; rxq_tag[rx_cnt] = store_tag; rx_cnt = rx_cnt + 1;
        if (rx_cnt == FIFO_DEPTH) cov[B_RX_FULL] = cov[B_RX_FULL] + 1;
      end
      if ((m_err & w1c) != 3'b000)    cov[B_W1C]       = cov[B_W1C] + 1;
      if ((set_bits & w1c) != 3'b000) cov[B_SAME_EDGE] = cov[B_SAME_EDGE] + 1;
      m_err = (m_err & ~w1c) | set_bits;     // event wins over clear (REQ-20)
      if (w1c != 3'b000) w1c_recent = 1'b1;

      // TX push
      if (push_tx) begin
        if (tx_qlen == 0) tx_R = c;
        txq[tx_qlen] = push_byte; tx_qlen = tx_qlen + 1;
        if ((tx_popped ? tx_qlen - 1 : tx_qlen) == FIFO_DEPTH) cov[B_TX_FULL] = cov[B_TX_FULL] + 1;
      end
      // CTRL
      if (wr_a && paidx == 3) begin
        if (!m_ctrl[0] && n_ctrl[0]) tx_R = c;
        if (m_ctrl[0] && !n_ctrl[0] && in_frame_c && !tx_end_now) cov[B_TX_DIS] = cov[B_TX_DIS] + 1;
        if (m_ctrl[1] && !n_ctrl[1]) begin
          i = 0;
          while (i < ev_n) begin
            if (c < ev_open[i] - 1) begin ev_drop(i); cov[B_RX_DIS] = cov[B_RX_DIS] + 1; end
            else begin
              $display("NOTE tb: RX_EN cleared inside a store window at cycle %0d (stimulus guard)", c);
              i = i + 1;
            end
          end
        end
        m_ctrl = n_ctrl;
      end
      m_baud = n_baud;
      // TX frame end
      if (tx_end_now) begin
        tx_inframe = 1'b0; tx_R = c; tx_lastL = c;
        cov[X_TX_FRAMES] = cov[X_TX_FRAMES] + 1;
        if (tx_par == 2'b01) cov[B_PAR_EVEN] = cov[B_PAR_EVEN] + 1;
        if (tx_par == 2'b11) cov[B_PAR_ODD]  = cov[B_PAR_ODD] + 1;
      end
    end
  endtask

  always @(negedge clk) begin
    #4;
    c = cyc;
    if (!model_valid) begin
      if (rst_n === 1'b0) begin model_reset; model_valid = 1'b1; reset_next = 1'b1; end
    end else begin
      cycle_check;
      if (rst_n === 1'b0) begin
        if (tx_inframe || rx_line_busy || ev_n > 0) cov[B_RESET_MID] = cov[B_RESET_MID] + 1;
        model_reset; reset_next = 1'b1;
      end
    end
  end

  // --------------------------------------------------------------------------
  // stimulus helpers
  // --------------------------------------------------------------------------
  reg [31:0] rdv;
  reg        rde;

  task automatic wait_cyc(input integer n);
    integer q;
    begin
      for (q = 0; q < n; q = q + 1) @(negedge clk);
    end
  endtask

  // APB transfer, called at a negedge: setup now, access next cycle, then idle.
  // paddr[1:0] are randomised (spec: ignored).
  task apb_core(input w, input [4:0] a, input [31:0] d);
    begin
      psel = 1'b1; penable = 1'b0; pwrite = w;
      paddr = {a[4:2], 2'b00} | (rnd(4) & 3);
      pwdata = w ? d : $random(seed);
      @(negedge clk);
      penable = 1'b1;
      #3;
      rdv = prdata; rde = pslverr;
      @(negedge clk);
      psel = 1'b0; penable = 1'b0; pwrite = rnd(2); paddr = rnd(32); pwdata = $random(seed);
    end
  endtask

  task apb(input w, input [4:0] a, input [31:0] d);
    begin
      @(negedge clk);
      apb_core(w, a, d);
    end
  endtask

  task wr(input [4:0] a, input [31:0] d); apb(1'b1, a, d); endtask
  task rd(input [4:0] a);                 apb(1'b0, a, 32'h0); endtask

  // transfer whose access cycle is exactly cycle A
  task apb_at(input integer A, input w, input [4:0] a, input [31:0] d);
    begin
      @(negedge clk);
      if (cyc > A - 1) $display("NOTE tb: apb_at late cyc=%0d target=%0d", cyc, A);
      while (cyc < A - 1) @(negedge clk);
      apb_core(w, a, d);
    end
  endtask

  // back-to-back write then read (read setup in the cycle right after the write access)
  task apb_wr_rd(input [4:0] wa, input [31:0] wd, input [4:0] ra);
    begin
      @(negedge clk);
      psel = 1'b1; penable = 1'b0; pwrite = 1'b1; paddr = {wa[4:2], 2'b00} | (rnd(4) & 3); pwdata = wd;
      @(negedge clk);
      penable = 1'b1;
      @(negedge clk);
      psel = 1'b1; penable = 1'b0; pwrite = 1'b0; paddr = {ra[4:2], 2'b00} | (rnd(4) & 3); pwdata = $random(seed);
      @(negedge clk);
      penable = 1'b1;
      #3; rdv = prdata; rde = pslverr;
      @(negedge clk);
      psel = 1'b0; penable = 1'b0;
    end
  endtask

  task do_reset(input integer n);
    begin
      @(negedge clk);
      rst_n = 1'b0; psel = 1'b0; penable = 1'b0;
      wait_cyc(n);
      rst_n = 1'b1;
    end
  endtask

  function [7:0] rbyte(input integer dummy);
    integer r;
    begin
      r = rnd(10);
      if (r == 0) rbyte = 8'h00; else if (r == 1) rbyte = 8'hFF; else rbyte = rnd(256);
    end
  endfunction

  // TX frame start possible within a few cycles (config writes must avoid this window)
  function tx_start_soon(input integer dummy);
    tx_start_soon = m_ctrl[0] && (tx_qlen > 0) && (!tx_inframe || (cyc >= tx_L - tx_D - 8));
  endfunction

  // no RX outcome window near: RXDATA reads and STATUS writes are unambiguous
  function rx_quiet(input integer dummy);
    integer q;
    begin
      rx_quiet = 1'b1;
      for (q = 0; q < ev_n; q = q + 1)
        if ((cyc + 4 >= ev_open[q]) && (cyc <= ev_commit[q] + 2)) rx_quiet = 1'b0;
    end
  endfunction

  task wait_tx_idle;
    integer t;
    begin
      t = 0;
      while ((tx_inframe || (m_ctrl[0] && tx_qlen > 0)) && t < 400000) begin @(negedge clk); t = t + 1; end
      wait_cyc(3);
    end
  endtask

  task automatic wait_rx_idle;
    integer t;
    begin
      t = 0;
      while ((ev_n > 0 || rx_line_busy) && t < 400000) begin @(negedge clk); t = t + 1; end
      wait_cyc(2);
    end
  endtask

  // Drive one UART frame on rxd.  D = BAUD (model) at the falling edge.
  //   phase_ps >= 0 : align to a posedge then wait phase_ps; < 0 : start immediately
  //   jit != 0      : each bit lasts a whole number of cycles in [0.98D, 1.02D]
  //   extra_low     : with bad_stop, keep rxd low this many extra cycles
  task rx_send(input [7:0] data, input integer bad_par, input integer bad_stop, input integer jit,
               input integer phase_ps, input integer extra_low, input integer tag);
    integer nb, D, bi, j, sum_ss, open, commit, kind, lmin, lmax, mode, b2b;
    integer lens [0:10];
    reg [10:0] bits;
    reg pen, podd;
    begin
      if (phase_ps >= 0) begin
        @(posedge clk);
        #(phase_ps * 0.001);
      end
      b2b = (rx_end_rt == $realtime) && rx_prev_ok;
      D = m_baud; pen = m_ctrl[2]; podd = m_ctrl[3];
      nb = pen ? 11 : 10;
      bits = 11'h7FF;
      bits[0] = 1'b0;
      bits[8:1] = data;
      if (pen) begin
        bits[9]  = (podd ? ~(^data) : (^data)) ^ (bad_par != 0);
        bits[10] = (bad_stop == 0);
      end else
        bits[9] = (bad_stop == 0);
      lmin = (D * 98 + 99) / 100; lmax = (D * 102) / 100; mode = rnd(3);
      for (bi = 0; bi < 11; bi = bi + 1) begin
        if (jit == 0) lens[bi] = D;
        else if (mode == 0) lens[bi] = lmin;
        else if (mode == 1) lens[bi] = lmax;
        else lens[bi] = lmin + rnd(lmax - lmin + 1);
      end
      sum_ss = 0;
      for (bi = 0; bi < nb - 1; bi = bi + 1) sum_ss = sum_ss + lens[bi];
      kind = ((bad_stop != 0) ? 1 : 0) | ((pen && (bad_par != 0)) ? 2 : 0);
      open = cyc + ((sum_ss < (nb - 1) * D) ? sum_ss : (nb - 1) * D) - 1;
      commit = (cal_rel >= 0) ? cyc + cal_rel : cyc + nb * D + 5;
      cal_rel = -1;
      last_cf = cyc;
      if (m_ctrl[1]) begin
        rx_make_event(kind, data, open, commit, tag, D, {podd, pen}, jit);
        if (b2b) cov[B_RX_B2B] = cov[B_RX_B2B] + 1;
      end
      rx_line_busy = 1'b1;
      for (bi = 0; bi < nb; bi = bi + 1) begin
        rxd = bits[bi];
        for (j = 0; (j < lens[bi]) && !rx_abort; j = j + 1) #(CLKP);
      end
      if (bad_stop != 0)
        for (j = 0; (j < extra_low) && !rx_abort; j = j + 1) #(CLKP);
      rxd = 1'b1;
      rx_line_busy = 1'b0;
      rx_end_rt = $realtime;
      rx_prev_ok = (bad_stop == 0) && !rx_abort;
    end
  endtask

  // false start: low pulse of len cycles while the receiver is idle (REQ-14)
  task rx_pulse(input integer len, input integer phase_ps);
    begin
      @(posedge clk);
      #(phase_ps * 0.001);
      if (m_ctrl[1] && m_baud >= 16 && len * 4 <= m_baud) cov[B_RX_FALSE] = cov[B_RX_FALSE] + 1;
      rx_line_busy = 1'b1;
      rxd = 1'b0;
      #(CLKP * len);
      rxd = 1'b1;
      rx_line_busy = 1'b0;
      rx_prev_ok = 1'b0;
    end
  endtask

  task rx_frame(input [7:0] b, input integer tag);
    begin
      rx_send(b, 0, 0, 0, 100 + rnd(8800), 0, tag);
      wait_rx_idle;
    end
  endtask

  task drain_rx;
    integer t;
    begin
      t = 0;
      while ((rx_cnt > 0 || ev_n > 0) && t < 64) begin
        if (rx_quiet(0)) rd(A_RX); else wait_cyc(4);
        t = t + 1;
      end
    end
  endtask

  // --------------------------------------------------------------------------
  // calibrated same-edge scenarios
  // The DUT's store/error edge for a frame driven at a fixed phase is measured
  // by sweeping a STATUS read over the outcome window (one frame per trial);
  // the next identical frame then gets an APB access ending at exactly that edge.
  // --------------------------------------------------------------------------
  task cal_overrun;   // rx_overrun_with_read (REQ-17)
    integer r, found, lo_r, hi_r, D_, nb_, e_rel;
    begin
      tag_req = 17;
      drain_rx;
      wr(A_ST, 32'hE0);
      D_ = m_baud; nb_ = m_ctrl[2] ? 11 : 10;
      rx_frame(rbyte(0), 13); rx_frame(rbyte(0), 13); rx_frame(rbyte(0), 13);
      found = -1;
      lo_r = (nb_ - 1) * D_ - 2; hi_r = nb_ * D_ + 8;
      for (r = lo_r; (r <= hi_r) && (found < 0); r = r + 1) begin
        wait_cyc(2 * D_);
        fork
          rx_send(rbyte(0), 0, 0, 0, CAL_PHASE, 0, 13);
          begin
            wait (rx_line_busy == 1'b1);
            apb_at(last_cf + r, 1'b0, A_ST, 32'h0);
            if (rdv[2] === 1'b1) found = r;      // RX_FULL seen (count 3 -> 4)
          end
        join
        wait_rx_idle;
        rd(A_RX);                                  // back to 3 bytes
      end
      if (found < 0) $display("NOTE tb: overrun calibration found no store edge");
      else begin
        e_rel = found - 1;
        $display("NOTE tb: RX store edge ends cycle fall+%0d (D=%0d)", e_rel, D_);
        rx_frame(rbyte(0), 13);                    // FIFO full
        wait_cyc(2 * D_);
        cal_rel = e_rel;
        fork
          rx_send(8'hE7, 0, 0, 0, CAL_PHASE, 0, 13);
          begin
            wait (rx_line_busy == 1'b1);
            apb_at(last_cf + e_rel, 1'b0, A_RX, 32'h0);  // read ends at the store edge
          end
        join
        wait_rx_idle;
        rd(A_ST);
        tag_req = 16;
        rd(A_RX); rd(A_RX); rd(A_RX); rd(A_RX);
        tag_req = 20;
        wr(A_ST, 32'h20); rd(A_ST);
      end
      tag_req = 0;
    end
  endtask

  task cal_ferr;      // error_set_and_clear_same_edge (REQ-20)
    integer r, found, lo_r, hi_r, D_, nb_, e_rel;
    begin
      tag_req = 20;
      wr(A_ST, 32'hE0);
      D_ = m_baud; nb_ = m_ctrl[2] ? 11 : 10;
      found = -1;
      lo_r = (nb_ - 1) * D_ - 2; hi_r = nb_ * D_ + 8;
      for (r = lo_r; (r <= hi_r) && (found < 0); r = r + 1) begin
        wait_cyc(2 * D_);
        fork
          rx_send(rbyte(0), 0, 1, 0, CAL_PHASE, 0, 18);
          begin
            wait (rx_line_busy == 1'b1);
            apb_at(last_cf + r, 1'b0, A_ST, 32'h0);
            if (rdv[6] === 1'b1) found = r;
          end
        join
        wait_rx_idle;
        wr(A_ST, 32'h40);
      end
      if (found < 0) $display("NOTE tb: framing-error calibration found no set edge");
      else begin
        e_rel = found - 1;
        $display("NOTE tb: FRAME_ERR set edge ends cycle fall+%0d (D=%0d)", e_rel, D_);
        wait_cyc(2 * D_);
        cal_rel = e_rel;
        fork
          rx_send(8'h5C, 0, 1, 0, CAL_PHASE, 0, 18);
          begin
            wait (rx_line_busy == 1'b1);
            apb_at(last_cf + e_rel, 1'b1, A_ST, 32'h40);   // W1C at the set edge
          end
        join
        wait_rx_idle;
        rd(A_ST);                 // FRAME_ERR must be 1 (event wins)
        wr(A_ST, 32'h40); rd(A_ST);
      end
      tag_req = 0;
    end
  endtask

  // --------------------------------------------------------------------------
  // random stress
  // --------------------------------------------------------------------------
  task rand_cfg_lock;
    integer t;
    begin
      cfg_lock = 1'b1; t = 0;
      while (rx_inflight && t < 40000) begin @(negedge clk); t = t + 1; end
    end
  endtask

  task rand_ctrl_write;
    reg [6:0] v;
    begin
      rand_cfg_lock;
      v = rnd(128);
      v[0] = (rnd(10) < 8);
      v[1] = (rnd(10) < 9);
      if (tx_start_soon(0)) begin v[3:2] = m_ctrl[3:2]; if (m_ctrl[0]) v[0] = 1'b1; end
      wr(A_CTRL, ($random(seed) & 32'hFFFFFF80) | v);
      cfg_lock = 1'b0;
    end
  endtask

  task rand_baud_write;
    integer r, v;
    begin
      rand_cfg_lock;
      r = rnd(10);
      if (r == 0) v = rnd(4); else if (r < 3) v = 16 + rnd(25); else v = 4 + rnd(12);
      if (!tx_start_soon(0)) wr(A_BAUD, ($random(seed) & 32'hFFFF0000) | v);
      else rd(A_BAUD);
      cfg_lock = 1'b0;
    end
  endtask

  task rx_random_thread;
    integer nfr, f, kind, D, bp, bs;
    begin
      while (stress_on) begin
        wait_cyc(1 + rnd(60));
        if (stress_on && !cfg_lock && m_baud <= 48 && rst_n === 1'b1) begin
          rx_inflight = 1'b1;
          wait_cyc(8);
          D = m_baud;
          if (D >= 16 && rnd(6) == 0) begin
            rx_pulse(1 + rnd(D / 4), 100 + rnd(8800));
            wait_cyc(D + 2 + rnd(D));
          end
          nfr = 1 + rnd(3);
          for (f = 0; f < nfr; f = f + 1) begin
            if (m_baud > 48) f = nfr;
            else begin
              kind = rnd(100);
              bp = (kind >= 80 && kind < 90); bs = (kind >= 90);
              rx_send(rbyte(0), bp, bs, 0, (f == 0) ? 100 + rnd(8800) : -1, bs ? rnd(2 * D) : 0, 13);
              if (bs) wait_cyc(2 * D + 2);
            end
          end
          wait_cyc(D + 12);
          rx_inflight = 1'b0;
        end
      end
    end
  endtask

  task random_stress(input integer ncyc);
    integer endc, op;
    begin
      endc = cyc + ncyc;
      while (cyc < endc) begin
        op = rnd(100);
        if (op < 18) begin
          if (tx_qlen >= FIFO_DEPTH && rnd(3) != 0) rd(A_ST); else wr(A_TX, rbyte(0));
        end
        else if (op < 30) rd(A_ST);
        else if (op < 42) begin if (rx_quiet(0)) rd(A_RX); else rd(A_ST); end
        else if (op < 47) begin if (rx_quiet(0)) wr(A_ST, $random(seed)); else rd(A_CTRL); end
        else if (op < 53) rand_ctrl_write;
        else if (op < 56) rand_baud_write;
        else if (op < 60) apb(rnd(2), 5'h14 + rnd(12), $random(seed));
        else if (op < 63) begin if (rnd(2)) rd(A_TX); else wr(A_RX, $random(seed)); end
        else if (op < 64) begin
          if (rnd(4) == 0) begin
            do_reset(1 + rnd(3));
            rand_cfg_lock;
            wr(A_BAUD, 4 + rnd(20));
            wr(A_CTRL, 32'h03 | (rnd(128) & 32'h7C));
            cfg_lock = 1'b0;
          end
        end
        else wait_cyc(1 + rnd(12));
      end
    end
  endtask

  // --------------------------------------------------------------------------
  // report (output contract)
  // --------------------------------------------------------------------------
  task report;
    integer r, b, allok;
    begin
      allok = !watchdog_hit;
      for (r = 1; r <= NREQ; r = r + 1) begin
        if (req_chk[r] == 0 || req_err[r] != 0) allok = 0;
        if (req_chk[r] > 0 && req_err[r] == 0)
          $display("REQ_RESULT REQ-%0d PASS checks=%0d errors=%0d", r, req_chk[r], req_err[r]);
        else
          $display("REQ_RESULT REQ-%0d FAIL checks=%0d errors=%0d", r, req_chk[r], req_err[r]);
      end
      $display("COVER tx_fifo_full %0d",                  cov[B_TX_FULL]);
      $display("COVER tx_write_when_full %0d",            cov[B_TX_WR_FULL]);
      $display("COVER tx_back_to_back %0d",               cov[B_TX_B2B]);
      $display("COVER tx_disable_mid_frame %0d",          cov[B_TX_DIS]);
      $display("COVER rx_fifo_full %0d",                  cov[B_RX_FULL]);
      $display("COVER rx_read_when_empty %0d",            cov[B_RX_RD_EMPTY]);
      $display("COVER rx_back_to_back %0d",               cov[B_RX_B2B]);
      $display("COVER rx_overrun %0d",                    cov[B_RX_OVR]);
      $display("COVER rx_overrun_with_read %0d",          cov[B_RX_OVR_RD]);
      $display("COVER rx_frame_error %0d",                cov[B_RX_FERR]);
      $display("COVER rx_parity_error %0d",               cov[B_RX_PERR]);
      $display("COVER rx_false_start %0d",                cov[B_RX_FALSE]);
      $display("COVER rx_baud_mismatch %0d",              cov[B_RX_MISM]);
      $display("COVER rx_disable_mid_frame %0d",          cov[B_RX_DIS]);
      $display("COVER parity_even_frame %0d",             cov[B_PAR_EVEN]);
      $display("COVER parity_odd_frame %0d",              cov[B_PAR_ODD]);
      $display("COVER baud_change_between_frames %0d",    cov[B_BAUD_CHG]);
      $display("COVER baud_below_min %0d",                cov[B_BAUD_LOW]);
      $display("COVER invalid_address %0d",               cov[B_INVALID]);
      $display("COVER status_w1c_clear %0d",              cov[B_W1C]);
      $display("COVER error_set_and_clear_same_edge %0d", cov[B_SAME_EDGE]);
      $display("COVER irq_rxne %0d",                      cov[B_IRQ_RXNE]);
      $display("COVER irq_txe %0d",                       cov[B_IRQ_TXE]);
      $display("COVER irq_err %0d",                       cov[B_IRQ_ERR]);
      $display("COVER reset_mid_frame %0d",               cov[B_RESET_MID]);
      for (b = 0; b <= B_RESET_MID; b = b + 1) if (cov[b] == 0) allok = 0;
      // extra (non-spec) bins
      $display("COVER x_apb_write_access %0d",            cov[X_APB_WR]);
      $display("COVER x_apb_read_access %0d",             cov[X_APB_RD]);
      $display("COVER x_tx_write_full_pop_race %0d",      cov[X_TX_POP_RACE]);
      $display("COVER x_tx_frames %0d",                   cov[X_TX_FRAMES]);
      $display("COVER x_rx_frames_resolved %0d",          cov[X_RX_FRAMES]);
      if (allok) $display("SUMMARY PASS"); else $display("SUMMARY FAIL");
    end
  endtask

  initial begin
    #(64'd40000000);   // 4,000,000 cycles
    watchdog_hit = 1'b1;
    $display("CHECK_FAIL watchdog expired at cycle %0d", cyc);
    report;
    $finish;
  end

  // --------------------------------------------------------------------------
  // main sequence
  // --------------------------------------------------------------------------
  integer n, a, b;
  initial begin
    for (n = 0; n <= NREQ; n = n + 1) begin req_chk[n] = 0; req_err[n] = 0; end
    for (n = 0; n < NBIN; n = n + 1) cov[n] = 0;
    nfail = 0; tag_req = 0; watchdog_hit = 1'b0;
    model_valid = 1'b0; reset_next = 1'b0; last_commit_c = -100;
    rx_line_busy = 1'b0; rx_abort = 1'b0; rx_inflight = 1'b0; cfg_lock = 1'b0; stress_on = 1'b0;
    cal_rel = -1; last_cf = 0; cur_jit = 0; rx_end_rt = -1.0; rx_prev_ok = 1'b0;
    ev_n = 0; tx_inframe = 1'b0;
    if (!$value$plusargs("seed=%d", seed)) seed = 1;
    $display("TB tb_uart_apb seed=%0d", seed);

    rst_n = 1'b0; psel = 1'b0; penable = 1'b0; pwrite = 1'b0; paddr = 0; pwdata = 0; rxd = 1'b1;
    wait_cyc(4);
    rst_n = 1'b1;

    // ---------------- P0 reset values (REQ-1) ----------------
    tag_req = 1;
    rd(A_ST); rd(A_CTRL); rd(A_BAUD); rd(A_RX); rd(A_TX);
    tag_req = 0;

    // ---------------- P1 register map / APB (REQ-2,3,4,5,22) ----------------
    tag_req = 3;
    wr(A_CTRL, 32'hFFFFFFFC); rd(A_CTRL);              // TX_EN=RX_EN=0, upper bits ignored
    wr(A_CTRL, 32'h0); rd(A_CTRL);
    apb_wr_rd(A_CTRL, 32'h54, A_CTRL);                 // write visible in the next transfer
    apb_wr_rd(A_CTRL, 32'h20, A_ST);                   // IE_TXE with TX empty -> irq
    wr(A_CTRL, 32'h0);
    wr(A_BAUD, 32'hABCD0010); rd(A_BAUD);
    wr(A_BAUD, 32'h0000FFFF); rd(A_BAUD);
    wr(A_RX, 32'h5A); rd(A_ST); rd(A_TX); rd(A_RX);
    tag_req = 22;
    for (n = 0; n < 4; n = n + 1) begin wr(A_BAUD, n); rd(A_BAUD); end
    wr(A_BAUD, 32'h00030000); rd(A_BAUD);              // [15:0]=0 -> rejected
    wr(A_BAUD, 32'h4); rd(A_BAUD);                     // DIV_MIN accepted
    wr(A_BAUD, 32'h00010005); rd(A_BAUD);
    tag_req = 2;
    apb_wr_rd(A_BAUD, 32'h7, A_BAUD);
    tag_req = 4;
    for (a = 5'h14; a <= 5'h1F; a = a + 1) begin rd(a); wr(a, $random(seed)); end
    rd(A_CTRL); rd(A_BAUD); rd(A_ST); rd(A_RX);
    tag_req = 5;
    wr(A_ST, 32'h0000001F); rd(A_ST);
    wr(A_ST, 32'hFFFFFF1F); rd(A_ST);
    tag_req = 0;

    // ---------------- P2 TX at DIV_RESET (REQ-8,9,10,12) ----------------
    tag_req = 9;
    wr(A_BAUD, DIV_RESET);
    wr(A_CTRL, 32'h01);
    wr(A_TX, 32'hA5);
    for (n = 0; n < 6; n = n + 1) begin wait_cyc(1500); rd(A_ST); end
    wait_tx_idle;
    tag_req = 0;

    // ---------------- P3 TX FIFO full / write when full / back-to-back (REQ-6,7,10) ----------------
    wr(A_BAUD, 8); wr(A_CTRL, 32'h0);
    tag_req = 6;
    rd(A_ST);
    wr(A_TX, 8'h00); rd(A_ST);
    wr(A_TX, 8'hFF); wr(A_TX, 8'h55); rd(A_ST);
    wr(A_TX, 8'h80); rd(A_ST);                         // full
    wr(A_TX, 8'h3C); rd(A_ST);                         // dropped, pslverr
    wait_cyc(50); rd(A_ST);                            // TX_EN=0: nothing sent (REQ-11)
    tag_req = 10;
    wr(A_CTRL, 32'h21);                                // TX_EN + IE_TXE
    while (tx_qlen > 0 || tx_inframe) begin rd(A_ST); wait_cyc(rnd(4)); end
    wait_tx_idle; rd(A_ST);
    tag_req = 0;

    // ---------------- P4 TX parity (REQ-8) ----------------
    tag_req = 8;
    wr(A_CTRL, 32'h05);                                // even
    wr(A_TX, 8'h01); wr(A_TX, 8'h03); wait_tx_idle;
    wr(A_CTRL, 32'h0D);                                // odd
    wr(A_TX, 8'h01); wr(A_TX, 8'hFE); wait_tx_idle;
    wr(A_TX, 8'h77);                                   // parity change mid-frame: frame keeps odd
    wait (tx_inframe == 1'b1); while (cyc < tx_S + 12) @(negedge clk);
    wr(A_CTRL, 32'h05);
    wait_tx_idle;
    wr(A_CTRL, 32'h01);
    tag_req = 0;

    // ---------------- P5 TX disable mid-frame (REQ-11) ----------------
    tag_req = 11;
    wr(A_TX, 8'h11); wr(A_TX, 8'h22); wr(A_TX, 8'h33);
    wait (tx_inframe == 1'b1); while (cyc < tx_S + 6) @(negedge clk);
    wr(A_CTRL, 32'h00);
    wait_cyc(300); rd(A_ST);
    wr(A_CTRL, 32'h01);
    wait_tx_idle; rd(A_ST);
    tag_req = 0;

    // ---------------- P6 BAUD change and DIV_MIN on TX (REQ-9,22) ----------------
    tag_req = 22;
    wr(A_TX, 8'hC3);
    wait (tx_inframe == 1'b1); while (cyc < tx_S + 10) @(negedge clk);
    wr(A_BAUD, 12);                                    // frame in progress keeps D=8
    wr(A_TX, 8'h3C); wait_tx_idle;
    wr(A_BAUD, 4); wr(A_TX, 8'h96); wr(A_TX, 8'h01); wait_tx_idle;
    wr(A_BAUD, 5); wr(A_TX, 8'h69); wait_tx_idle;
    wr(A_BAUD, 8);
    tag_req = 0;

    // ---------------- P7 TX_BUSY polling (REQ-12) ----------------
    tag_req = 12;
    for (n = 0; n < 6; n = n + 1) begin
      wr(A_TX, rbyte(0));
      for (b = 0; b < 40; b = b + 1) begin rd(A_ST); wait_cyc(rnd(3)); end
    end
    wait_tx_idle; rd(A_ST);
    wr(A_CTRL, 32'h00);
    wr(A_TX, 8'h42); rd(A_ST); rd(A_ST);               // queued, TX_EN=0: busy 0
    wr(A_CTRL, 32'h01);
    for (b = 0; b < 10; b = b + 1) rd(A_ST);
    wait_tx_idle;
    tag_req = 0;

    // ---------------- P8 RX basic (REQ-13,15,16) ----------------
    wr(A_BAUD, 16); wr(A_CTRL, 32'h02);
    tag_req = 13;
    rx_frame(8'h00, 13); rd(A_ST); rd(A_RX);
    rx_frame(8'hFF, 13); rd(A_ST); rd(A_RX);
    for (n = 0; n < 4; n = n + 1) begin rx_frame(rbyte(0), 13); rd(A_ST); rd(A_RX); end
    tag_req = 15;
    fork
      rx_send(8'h3C, 0, 0, 0, 100 + rnd(8800), 0, 15);
      begin wait (rx_line_busy == 1'b1); apb_at(last_cf + 10 * 16 + 6, 1'b0, A_ST, 32'h0); end
    join
    wait_rx_idle; rd(A_RX);
    tag_req = 16;
    rd(A_RX);                                          // empty -> pslverr
    tag_req = 13;
    wr(A_BAUD, DIV_MIN);                               // D = DIV_MIN
    for (n = 0; n < 3; n = n + 1) rx_frame(rbyte(0), 13);
    rd(A_RX); rd(A_RX); rd(A_RX);
    wr(A_BAUD, 5);
    rx_frame(rbyte(0), 13); rd(A_RX);
    wr(A_BAUD, 16);
    tag_req = 0;

    // ---------------- P9 RX FIFO full / overrun (REQ-7,16,17,20,23) ----------------
    tag_req = 17;
    wr(A_CTRL, 32'h52);                                // RX_EN, IE_RXNE, IE_ERR
    for (n = 0; n < 4; n = n + 1) rx_frame(rbyte(0), 13);
    rd(A_ST);
    rx_frame(8'hEE, 13);                               // overrun
    rd(A_ST); wait_cyc(30); rd(A_ST);                  // sticky
    tag_req = 16;
    for (n = 0; n < 5; n = n + 1) rd(A_RX);            // 4 bytes then empty
    rd(A_ST);
    tag_req = 20;
    wr(A_ST, 32'h20); rd(A_ST);
    tag_req = 0;

    // ---------------- P10 RX back-to-back (REQ-13) ----------------
    tag_req = 13;
    wr(A_CTRL, 32'h02);
    rx_send(8'h31, 0, 0, 0, 100 + rnd(8800), 0, 13);
    rx_send(8'h32, 0, 0, 0, -1, 0, 13);
    rx_send(8'h33, 0, 0, 0, -1, 0, 13);
    rx_send(8'h34, 0, 0, 0, -1, 0, 13);
    wait_rx_idle;
    for (n = 0; n < 4; n = n + 1) rd(A_RX);
    tag_req = 0;

    // ---------------- P11 RX parity / framing errors (REQ-18,19,20) ----------------
    tag_req = 19;
    wr(A_CTRL, 32'h46);                                // RX_EN, PARITY_EN even, IE_ERR
    rx_frame(8'h01, 13); rx_frame(8'h03, 13);
    rd(A_RX); rd(A_RX);
    rx_send(8'h07, 1, 0, 0, 100 + rnd(8800), 0, 19); wait_rx_idle;
    rd(A_ST); rd(A_RX);
    wr(A_CTRL, 32'h4E);                                // odd
    rx_frame(8'h01, 13); rx_frame(8'hFE, 13);
    rd(A_RX); rd(A_RX);
    rx_send(8'h80, 1, 0, 0, 100 + rnd(8800), 0, 19); wait_rx_idle;
    rd(A_ST);
    tag_req = 20;
    wr(A_ST, 32'h80); rd(A_ST);
    tag_req = 18;
    rx_send(8'hA0, 0, 1, 0, 100 + rnd(8800), 0, 18); wait_rx_idle;
    rd(A_ST); rd(A_RX);
    rx_send(8'hA1, 0, 1, 0, 100 + rnd(8800), 64, 18); wait_rx_idle;   // line held low after stop
    wait_cyc(32);
    rx_frame(8'h5D, 18);                               // next frame received normally
    rd(A_ST); rd(A_RX);
    rx_send(8'hA2, 1, 1, 0, 100 + rnd(8800), 0, 18); wait_rx_idle;    // both errors
    rd(A_ST);
    tag_req = 20;
    wr(A_ST, 32'h20); rd(A_ST);                        // bits written 0 unchanged
    wr(A_ST, 32'h40); rd(A_ST);
    wr(A_ST, 32'hFFFFFF80); rd(A_ST);
    wr(A_CTRL, 32'h02);
    rx_send(8'hB4, 0, 1, 0, 100 + rnd(8800), 0, 18); wait_rx_idle;    // no parity, stop=0
    wait_cyc(32);
    rx_frame(8'h4B, 18); rd(A_ST); rd(A_RX);
    wr(A_ST, 32'hE0); rd(A_ST);
    tag_req = 0;

    // ---------------- P12 RX false start (REQ-14) ----------------
    tag_req = 14;
    wr(A_BAUD, 32);
    for (n = 1; n <= 8; n = n * 2) begin
      rx_pulse(n, 100 + rnd(8800)); wait_cyc(40); rd(A_ST);
    end
    rx_frame(8'hC6, 14); rd(A_ST); rd(A_RX);
    rx_pulse(8, 100 + rnd(8800)); wait_cyc(33);
    rx_send(8'h6C, 0, 0, 0, -1, 0, 14); wait_rx_idle; rd(A_ST); rd(A_RX);
    tag_req = 0;

    // ---------------- P13 RX baud mismatch +-2 % (REQ-13) ----------------
    tag_req = 13;
    wr(A_BAUD, 100);
    for (n = 0; n < 6; n = n + 1) begin
      rx_send(rbyte(0), 0, 0, 1, 100 + rnd(8800), 0, 13); wait_rx_idle; rd(A_RX);
    end
    wr(A_CTRL, 32'h06);
    rx_send(rbyte(0), 0, 0, 1, 100 + rnd(8800), 0, 13);
    rx_send(rbyte(0), 0, 0, 1, -1, 0, 13);
    wait_rx_idle; rd(A_RX); rd(A_RX);
    wr(A_BAUD, 200);
    rx_send(rbyte(0), 0, 0, 1, 100 + rnd(8800), 0, 13); wait_rx_idle; rd(A_RX);
    wr(A_CTRL, 32'h02);
    rx_send(rbyte(0), 0, 0, 1, 100 + rnd(8800), 0, 13); wait_rx_idle; rd(A_RX);
    tag_req = 0;

    // ---------------- P14 RX disable (REQ-21) ----------------
    tag_req = 21;
    wr(A_BAUD, 16);
    rx_frame(8'h17, 21); rd(A_ST);
    fork
      rx_send(8'h5A, 0, 0, 0, 100 + rnd(8800), 0, 21);
      begin wait (rx_line_busy == 1'b1); wait_cyc(64); wr(A_CTRL, 32'h00); end
    join
    wait_rx_idle; wait_cyc(20); rd(A_ST);
    rx_frame(8'h66, 21); rd(A_ST);                     // RX_EN=0: not received
    rd(A_RX);                                          // older byte kept
    wr(A_CTRL, 32'h02); wait_cyc(40);
    rx_frame(8'h99, 21); rd(A_ST); rd(A_RX);
    tag_req = 0;

    // ---------------- P15 BAUD write during RX frame (REQ-22) ----------------
    tag_req = 22;
    fork
      rx_send(8'h81, 0, 0, 0, 100 + rnd(8800), 0, 22);
      begin wait (rx_line_busy == 1'b1); wait_cyc(50); wr(A_BAUD, 20); end
    join
    wait_rx_idle; rd(A_RX);
    rx_frame(8'h18, 22); rd(A_RX);
    tag_req = 0;

    // ---------------- P16 irq sources (REQ-23) ----------------
    tag_req = 23;
    wr(A_BAUD, 8);
    wr(A_CTRL, 32'h12);                                // IE_RXNE
    rx_frame(8'h21, 13); wait_cyc(10); rd(A_RX); wait_cyc(5);
    wr(A_CTRL, 32'h42);                                // IE_ERR
    rx_send(8'h22, 0, 1, 0, 100 + rnd(8800), 0, 18); wait_rx_idle; wait_cyc(10);
    wr(A_ST, 32'h40); wait_cyc(5);
    wr(A_CTRL, 32'h21);                                // IE_TXE, TX_EN
    wr(A_TX, 8'h12); wr(A_TX, 8'h34); wait_tx_idle; wait_cyc(10);
    wr(A_CTRL, 32'h02);
    tag_req = 0;

    // ---------------- P17 calibrated same-edge scenarios (REQ-17,20) ----------------
    wr(A_BAUD, 8); wr(A_CTRL, 32'h52);
    cal_overrun;
    cal_ferr;
    drain_rx; wr(A_ST, 32'hE0);

    // ---------------- P18 reset mid-frame (REQ-1) ----------------
    wr(A_BAUD, 16); wr(A_CTRL, 32'h03);
    wr(A_TX, 8'h5A); wr(A_TX, 8'h6B);
    fork
      rx_send(8'h77, 0, 0, 0, 100 + rnd(8800), 0, 13);
      begin
        wait (rx_line_busy == 1'b1); wait_cyc(40);
        tag_req = 1;
        do_reset(2);
        rx_abort = 1'b1;
      end
    join
    rx_abort = 1'b0;
    rd(A_ST); rd(A_CTRL); rd(A_BAUD); rd(A_RX); rd(A_TX);
    tag_req = 0;
    wr(A_BAUD, 8); wr(A_CTRL, 32'h03);
    wait_cyc(40);
    wr(A_TX, 8'h42);
    rx_frame(8'h24, 13);
    wait_tx_idle; rd(A_ST); rd(A_RX);

    // ---------------- P19 random stress ----------------
    wr(A_BAUD, 10); wr(A_CTRL, 32'h7B);
    stress_on = 1'b1;
    fork
      rx_random_thread;
      begin random_stress(60000); stress_on = 1'b0; end
    join
    cfg_lock = 1'b0;

    // ---------------- P20 drain ----------------
    wait_rx_idle;
    wr(A_BAUD, 8);
    wr(A_CTRL, 32'h03);
    wait_tx_idle;
    drain_rx;
    rd(A_ST);
    wait_cyc(10);
    ckr(8,  "drain_tx", (tx_qlen == 0) && !tx_inframe, 0, tx_qlen, "TX bytes left at end of test");
    ckr(16, "drain_rx", (rx_cnt == 0) && (ev_n == 0), 0, rx_cnt, "RX bytes/outcomes left at end");

    report;
    $finish;
  end

endmodule
