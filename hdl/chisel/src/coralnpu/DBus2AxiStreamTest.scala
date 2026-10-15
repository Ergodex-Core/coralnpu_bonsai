// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package coralnpu

import chisel3._
import chisel3.simulator.scalatest.ChiselSim
import org.scalatest.freespec.AnyFreeSpec
import scala.collection.mutable

// Directed tests for DBus2AxiStream against a cycle-level AXI memory: reads
// answered `latency` cycles after AR, in order, one beat per cycle, any
// number outstanding; one write at a time, B on the cycle after its W beat.
class DBus2AxiStreamSpec extends AnyFreeSpec with ChiselSim {
  val p = new Parameters
  p.lsuDataBits = 128
  p.ebusPrefetchStreams = 4
  p.ebusPrefetchDepth = 8
  val Row  = 16
  val Base = BigInt("80000000", 16)

  class Bench(dut: DBus2AxiStream, latency: Int) {
    val mem      = mutable.Map[BigInt, Byte]().withDefault(a => (a & 0xff).toByte)
    case class Rd(addr: BigInt, len: Int, ready: Long, var beat: Int)
    val reads    = mutable.Queue[Rd]()
    val ars      = mutable.ArrayBuffer[(BigInt, Int, Int)]() // addr, len, size
    var maxReads = 0
    var cycle    = 0L
    var wAddr: Option[BigInt] = None
    var bPending = false

    def row(addr: BigInt): BigInt =
      (0 until Row).map(i => BigInt(mem(addr / Row * Row + i) & 0xff) << (8 * i)).sum

    // Drives the AXI target for one cycle, then steps the clock. Returns
    // whether the bridge accepted the DBus request this cycle.
    def step(): Boolean = {
      val axi = dut.io.axi
      axi.read.addr.ready.poke(true.B)
      axi.write.addr.ready.poke(wAddr.isEmpty.B)
      axi.write.data.ready.poke(wAddr.nonEmpty.B)
      axi.write.resp.valid.poke(bPending.B)
      axi.write.resp.bits.resp.poke(0.U)
      val head  = reads.headOption.filter(_.ready <= cycle)
      axi.read.data.valid.poke(head.nonEmpty.B)
      head.foreach { r =>
        axi.read.data.bits.data.poke(row(r.addr + r.beat * Row).U)
        axi.read.data.bits.last.poke((r.beat == r.len).B)
        axi.read.data.bits.resp.poke(0.U)
      }
      val rFire  = head.nonEmpty && axi.read.data.ready.peek().litToBoolean
      val arFire = axi.read.addr.valid.peek().litToBoolean
      val ar     = (axi.read.addr.bits.addr.peek().litValue, axi.read.addr.bits.len.peek().litValue.toInt,
        axi.read.addr.bits.size.peek().litValue.toInt)
      val awFire = wAddr.isEmpty && axi.write.addr.valid.peek().litToBoolean
      val aw     = axi.write.addr.bits.addr.peek().litValue
      val wFire  = wAddr.nonEmpty && axi.write.data.valid.peek().litToBoolean
      val (wData, wStrb) = (axi.write.data.bits.data.peek().litValue, axi.write.data.bits.strb.peek().litValue)
      val bFire  = bPending && axi.write.resp.ready.peek().litToBoolean
      val accepted = dut.io.dbus.valid.peek().litToBoolean && dut.io.dbus.ready.peek().litToBoolean
      dut.clock.step()
      cycle += 1
      if (rFire) { val r = reads.head; if (r.beat == r.len) reads.dequeue() else r.beat += 1 }
      if (arFire) { ars += ar; reads.enqueue(Rd(ar._1 / Row * Row, ar._2, cycle + latency, 0)) }
      maxReads = maxReads.max(reads.size)
      if (awFire) wAddr = Some(aw)
      if (wFire) {
        val rowAddr = wAddr.get / Row * Row
        for (i <- 0 until Row if wStrb.testBit(i)) mem(rowAddr + i) = ((wData >> (8 * i)) & 0xff).toByte
        wAddr = None; bPending = true
      }
      if (bFire) bPending = false
      accepted
    }

    def access(addr: BigInt, size: Int, write: Boolean = false, data: BigInt = 0): BigInt = {
      dut.io.dbus.valid.poke(true.B)
      dut.io.dbus.write.poke(write.B)
      dut.io.dbus.addr.poke(addr.U)
      dut.io.dbus.size.poke(size.U)
      dut.io.dbus.wdata.poke(data.U(128.W))
      dut.io.dbus.wmask.poke(if (write) ((BigInt(1) << size) - 1) << (addr % Row).toInt else BigInt(0))
      var n = 0
      while (!step()) { n += 1; assert(n < 1000, f"no response for $addr%x") }
      dut.io.dbus.valid.poke(false.B)
      dut.io.dbus.rdata.peek().litValue // DBus read data appears the cycle after ready
    }
    def read(addr: BigInt): BigInt = access(addr, Row)

    dut.io.flush.poke(false.B)
    dut.io.dbus.valid.poke(false.B)
    dut.io.dbus.pc.poke(0.U)
    dut.io.dbus.adrx.poke(0.U)
  }

  "streams sequential reads with bursts, several in flight" in {
    simulate(new DBus2AxiStream(p, 0)) { dut =>
      val b     = new Bench(dut, latency = 20)
      val rows  = 64
      val start = b.cycle
      for (i <- 0 until rows) assert(b.read(Base + i * Row) == b.row(Base + i * Row), s"row $i")
      val cycles = b.cycle - start
      assert(b.ars.exists(_._2 > 0), "no multi-beat burst")
      assert(b.maxReads > 1, "never more than one burst in flight")
      assert(cycles < rows * 20 / 4, s"$cycles cycles for $rows rows: not prefetching")
    }
  }

  "a write to a buffered row is seen by the next read" in {
    simulate(new DBus2AxiStream(p, 0)) { dut =>
      val b = new Bench(dut, latency = 20)
      b.read(Base)                                  // starts a stream over Base..
      for (_ <- 0 until 40) b.step()                // let it fill
      val value = BigInt("deadbeef", 16)
      b.access(Base + Row + 4, 4, write = true, data = value << 32)
      assert(((b.read(Base + Row) >> 32) & 0xffffffffL) == value)
    }
  }

  "flush drops buffered rows" in {
    simulate(new DBus2AxiStream(p, 0)) { dut =>
      val b = new Bench(dut, latency = 20)
      b.read(Base)
      for (_ <- 0 until 40) b.step()
      for (i <- 0 until Row) b.mem(Base + Row + i) = 0x5a.toByte // host rewrites memory
      dut.io.flush.poke(true.B); b.step(); dut.io.flush.poke(false.B)
      assert(b.read(Base + Row) == b.row(Base + Row))
    }
  }

  "uncached reads keep their address and size, one beat" in {
    simulate(new DBus2AxiStream(p, 0)) { dut =>
      val b = new Bench(dut, latency = 5)
      b.access(BigInt("30014", 16), 4)
      b.access(BigInt("30014", 16), 4)
      assert(b.ars.toSeq == Seq((BigInt("30014", 16), 0, 2), (BigInt("30014", 16), 0, 2)))
    }
  }
}
