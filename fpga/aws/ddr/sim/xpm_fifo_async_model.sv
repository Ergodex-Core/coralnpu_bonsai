// Portable functional model for the specific equal-width FWFT XPM FIFO used by
// coral_ddr_cdc. Not AMD IP and never synthesized. It models Gray-pointer
// visibility/reset-busy latency, not metastability or routed CDC timing.
`ifndef SYNTHESIS
module xpm_fifo_async #(
    parameter FIFO_MEMORY_TYPE = "distributed",
    parameter integer FIFO_WRITE_DEPTH = 16,
    parameter integer WRITE_DATA_WIDTH = 32,
    READ_DATA_WIDTH = 32,
    parameter READ_MODE = "fwft",
    parameter integer FIFO_READ_LATENCY = 0,
    CDC_SYNC_STAGES = 3,
    parameter integer WR_DATA_COUNT_WIDTH = 5,
    RD_DATA_COUNT_WIDTH = 5,
    parameter USE_ADV_FEATURES = "0000",
    parameter integer SIM_ASSERT_CHK = 1
) (
    input wire rst,
    wr_clk,
    rd_clk,
    input wire [WRITE_DATA_WIDTH-1:0] din,
    input wire wr_en,
    rd_en,
    output wire [READ_DATA_WIDTH-1:0] dout,
    output wire full,
    empty,
    wr_rst_busy,
    rd_rst_busy,
    input wire sleep,
    injectsbiterr,
    injectdbiterr,
    output wire almost_empty,
    almost_full,
    data_valid,
    dbiterr,
    output wire overflow,
    prog_empty,
    prog_full,
    output wire [RD_DATA_COUNT_WIDTH-1:0] rd_data_count,
    output wire sbiterr,
    underflow,
    wr_ack,
    output wire [WR_DATA_COUNT_WIDTH-1:0] wr_data_count
);
  localparam integer A = $clog2(FIFO_WRITE_DEPTH);
  logic [WRITE_DATA_WIDTH-1:0] mem[0:FIFO_WRITE_DEPTH-1];
  logic [A:0] wbin = 0, rbin = 0, wgray = 0, rgray = 0;
  logic [A:0] r_sync[0:CDC_SYNC_STAGES-1];
  logic [A:0] w_sync[0:CDC_SYNC_STAGES-1];
  logic [CDC_SYNC_STAGES-1:0] wb = '1, rb = '1;
  wire [A:0] wn = wbin + 1'b1, rn = rbin + 1'b1;
  assign full = wgray == {~r_sync[CDC_SYNC_STAGES-1][A:A-1], r_sync[CDC_SYNC_STAGES-1][A-2:0]};
  assign empty = rgray == w_sync[CDC_SYNC_STAGES-1];
  assign dout = mem[rbin[A-1:0]];
  assign wr_rst_busy = |wb;
  assign rd_rst_busy = |rb;
  assign almost_empty = 0;
  assign almost_full = 0;
  assign data_valid = !empty && !rd_rst_busy;
  assign dbiterr = 0;
  assign overflow = wr_en && full;
  assign prog_empty = 0;
  assign prog_full = 0;
  assign rd_data_count = 0;
  assign sbiterr = 0;
  assign underflow = rd_en && empty;
  assign wr_ack = wr_en && !full && !wr_rst_busy;
  assign wr_data_count = 0;
  initial begin
    if (WRITE_DATA_WIDTH != READ_DATA_WIDTH || READ_MODE != "fwft" ||
        FIFO_READ_LATENCY != 0 || CDC_SYNC_STAGES < 2)
      $fatal(1, "Unsupported XPM model configuration");
    for (integer i = 0; i < CDC_SYNC_STAGES; i = i + 1) begin
      r_sync[i] = 0;
      w_sync[i] = 0;
    end
  end
  always @(posedge wr_clk or posedge rst) begin
    if (rst) begin
      wbin <= 0;
      wgray <= 0;
      wb <= '1;
      for (integer i = 0; i < CDC_SYNC_STAGES; i = i + 1) r_sync[i] <= 0;
    end else begin
      wb <= {wb[CDC_SYNC_STAGES-2:0], 1'b0};
      r_sync[0] <= rgray;
      for (integer i = 1; i < CDC_SYNC_STAGES; i = i + 1) r_sync[i] <= r_sync[i-1];
      if (wr_en && !full && !wr_rst_busy) begin
        mem[wbin[A-1:0]] <= din;
        wbin <= wn;
        wgray <= (wn >> 1) ^ wn;
      end
      if ((SIM_ASSERT_CHK != 0) && wr_en && (full || wr_rst_busy))
        $fatal(1, "FIFO overflow/busy write");
    end
  end
  always @(posedge rd_clk or posedge rst) begin
    if (rst) begin
      rbin <= 0;
      rgray <= 0;
      rb <= '1;
      for (integer i = 0; i < CDC_SYNC_STAGES; i = i + 1) w_sync[i] <= 0;
    end else begin
      rb <= {rb[CDC_SYNC_STAGES-2:0], 1'b0};
      w_sync[0] <= wgray;
      for (integer i = 1; i < CDC_SYNC_STAGES; i = i + 1) w_sync[i] <= w_sync[i-1];
      if (rd_en && !empty && !rd_rst_busy) begin
        rbin  <= rn;
        rgray <= (rn >> 1) ^ rn;
      end
      if ((SIM_ASSERT_CHK != 0) && rd_en && (empty || rd_rst_busy))
        $fatal(1, "FIFO underflow/busy read");
    end
  end
endmodule
`endif
