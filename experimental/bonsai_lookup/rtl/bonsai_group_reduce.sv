// Ordered RNE32 scaling/reduction. Requires pinned external fpnew_fma sources.
// One shared arithmetic unit, no fused operations, no host arithmetic shortcut.
module bonsai_group_reduce (
    input wire clk,
    input wire rst_n,
    input wire start_valid,
    output wire start_ready,
    input wire [3:0] start_m,
    input wire [5:0] start_n,
    input wire [5:0] start_groups,
    input wire [31:0] start_tag,
    input wire in_valid,
    output wire in_ready,
    input wire signed [31:0] in_dot,
    input wire [15:0] in_weight_scale,
    input wire [31:0] in_activation_scale,
    input wire [3:0] in_token,
    input wire [5:0] in_row,
    input wire [5:0] in_group,
    input wire in_error,
    output wire out_valid,
    input wire out_ready,
    output reg [31:0] out_value,
    output reg [3:0] out_token,
    output reg [5:0] out_row,
    output reg [5:0] out_group,
    output reg out_last,
    output reg out_error,
    output reg [4:0] out_fflags,
    output reg [31:0] out_tag,
    output reg done,
    output reg job_error
);
  localparam IDLE=0, ACCEPT=1, MUL_W=2, WAIT_W=3, MUL_A=4,
             WAIT_A=5, ADD=6, WAIT_ADD=7, EMIT=8;
  reg [3:0] state;
  reg [3:0] m, token;
  reg [5:0] n, groups, row, group_id;
  reg [31:0] accum[0:255];
  reg [4:0] accum_flags[0:255];
  reg [31:0] dot_fp, weight_fp, activation_fp, partial;
  reg  [7:0] index_q;
  wire [7:0] index_next = {token[2:0], row[4:0]};
  reg  [4:0] flags;
  wire fp_ready, fp_valid;
  wire [31:0] fp_result;
  fpnew_pkg::status_t fp_status;
  fpnew_pkg::operation_e fp_op;
  logic [2:0][31:0] operands;
  integer i;

  // Every supported dot (-16384..16384) converts exactly to FP32.
  function automatic [31:0] dot_to_fp(input logic signed [31:0] d);
    reg [31:0] mag, mant;
    reg [7:0] exponent;
    integer bitpos, j;
    begin
      mag = d[31] ? $unsigned(-d) : $unsigned(d);
      bitpos = 0;
      for (j = 0; j < 15; j = j + 1) if (mag[j]) bitpos = j;
      mant = mag << (23 - bitpos);
      exponent = 8'(127 + bitpos);
      dot_to_fp = mag == 0 ? 32'b0 : {d[31], exponent, mant[22:0]};
    end
  endfunction
  // Exact FP16-to-FP32 widening, including signed zero and subnormals.
  function automatic [31:0] half_to_fp(input logic [15:0] h);
    reg [10:0] mant;
    reg [ 7:0] exponent;
    integer j, shift;
    begin
      mant  = {1'b0, h[9:0]};
      shift = 0;
      if (h[14:10] == 0) begin
        for (j = 0; j < 10; j = j + 1) begin
          if (!mant[10] && mant != 0) begin
            mant  = mant << 1;
            shift = shift + 1;
          end
        end
        exponent   = 8'(113 - shift);
        half_to_fp = h[9:0] == 0 ? {h[15], 31'b0} : {h[15], exponent, mant[9:0], 13'b0};
      end else begin
        exponent   = h[14:10] == 31 ? 8'hff : 8'(int'(h[14:10]) + 112);
        half_to_fp = {h[15], exponent, h[9:0], 13'b0};
      end
    end
  endfunction

  assign start_ready = state == IDLE;
  assign in_ready = state == ACCEPT;
  assign out_valid = state == EMIT;
  always @* begin
    operands = '0;
    fp_op = fpnew_pkg::MUL;
    if (state == MUL_W) begin
      operands[0] = dot_fp;
      operands[1] = weight_fp;
    end
    if (state == MUL_A) begin
      operands[0] = partial;
      operands[1] = activation_fp;
    end
    if (state == ADD) begin
      fp_op = fpnew_pkg::ADD;
      operands[1] = accum[index_q];
      operands[2] = partial;
    end
  end
  fpnew_fma #(
      .FpFormat(fpnew_pkg::FP32),
      .NumPipeRegs(4),
      .PipeConfig(fpnew_pkg::DISTRIBUTED)
  ) arithmetic (
      .clk_i(clk),
      .rst_ni(rst_n),
      .operands_i(operands),
      .is_boxed_i(3'b111),
      .rnd_mode_i(fpnew_pkg::RNE),
      .op_i(fp_op),
      .op_mod_i(1'b0),
      .tag_i(1'b0),
      .mask_i(1'b1),
      .aux_i(1'b0),
      .in_valid_i(state == MUL_W || state == MUL_A || state == ADD),
      .in_ready_o(fp_ready),
      .flush_i(1'b0),
      .result_o(fp_result),
      .status_o(fp_status),
      .extension_bit_o(),
      .tag_o(),
      .mask_o(),
      .aux_o(),
      .out_valid_o(fp_valid),
      .out_ready_i(state == WAIT_W || state == WAIT_A || state == WAIT_ADD),
      .busy_o(),
      .reg_ena_i(4'b0),
      .early_out_valid_o()
  );
  always @(posedge clk) begin
    if (!rst_n) begin
      state <= IDLE;
      done <= 0;
      job_error <= 0;
      m <= 0;
      n <= 0;
      groups <= 0;
      token <= 0;
      row <= 0;
      group_id <= 0;
      index_q <= 0;
      dot_fp <= 0;
      weight_fp <= 0;
      activation_fp <= 0;
      partial <= 0;
      flags <= 0;
      out_value <= 0;
      out_token <= 0;
      out_row <= 0;
      out_group <= 0;
      out_last <= 0;
      out_error <= 0;
      out_fflags <= 0;
      out_tag <= 0;
    end else begin
      done <= 0;
      case (state)
        IDLE:
        if (start_valid) begin
          job_error <= 0;
          out_tag <= start_tag;
          token <= 0;
          row <= 0;
          group_id <= 0;
          if(start_m==0 || start_m>8 || start_n==0 || start_n>32 ||
              start_groups==0 || start_groups>48) begin
            done <= 1;
            job_error <= 1;
          end else begin
            m <= start_m;
            n <= start_n;
            groups <= start_groups;
            for (i = 0; i < 256; i = i + 1) begin
              accum[i] <= 0;
              accum_flags[i] <= 0;
            end
            state <= ACCEPT;
          end
        end
        ACCEPT:
        if (in_valid) begin
          out_token <= token;
          out_row <= row;
          out_group <= group_id;
          out_last <= group_id == groups - 1;
          out_error <= job_error;
          out_fflags <= 0;
          index_q <= index_next;
          flags <= 0;
          if(in_error || in_token!=token || in_row!=row || in_group!=group_id ||
              in_dot < -16384 || in_dot > 16384 || in_weight_scale[14:10]==31 ||
              in_activation_scale[31] || in_activation_scale[30:23]==255 ||
              in_activation_scale[30:0]==0) begin
            job_error <= 1;
            out_error <= 1;
            out_value <= 32'h7fc00000;
            state <= EMIT;
          end else begin
            dot_fp <= dot_to_fp(in_dot);
            weight_fp <= half_to_fp(in_weight_scale);
            activation_fp <= in_activation_scale;
            state <= MUL_W;
          end
        end
        MUL_W: if (fp_ready) state <= WAIT_W;
        WAIT_W:
        if (fp_valid) begin
          partial <= fp_result;
          flags   <= 5'(fp_status);
          state   <= MUL_A;
        end
        MUL_A: if (fp_ready) state <= WAIT_A;
        WAIT_A:
        if (fp_valid) begin
          partial <= fp_result;
          flags   <= flags | 5'(fp_status);
          state   <= ADD;
        end
        ADD: if (fp_ready) state <= WAIT_ADD;
        WAIT_ADD:
        if (fp_valid) begin
          accum[index_q] <= fp_result;
          out_value <= fp_result;
          out_fflags <= accum_flags[index_q] | flags | 5'(fp_status);
          accum_flags[index_q] <= accum_flags[index_q] | flags | 5'(fp_status);
          state <= EMIT;
        end
        EMIT:
        if (out_ready) begin
          if (row == n - 1) begin
            row <= 0;
            if (token == m - 1) begin
              token <= 0;
              if (group_id == groups - 1) begin
                done  <= 1;
                state <= IDLE;
              end else begin
                group_id <= group_id + 1;
                state <= ACCEPT;
              end
            end else begin
              token <= token + 1;
              state <= ACCEPT;
            end
          end else begin
            row   <= row + 1;
            state <= ACCEPT;
          end
        end
        default: state <= IDLE;
      endcase
    end
  end
endmodule
