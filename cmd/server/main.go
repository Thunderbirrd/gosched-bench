// Command server — тестируемый сервис (SUT) для характеризации планировщика Go.
//
// Два класса работы в одном процессе:
//   - LC: короткие запросы по TCP (или изнутри процесса при -lc-source=inproc);
//   - BE: фоновые CPU-задачи длиной q, пуассоновский поток внутри процесса.
//
// Управление окном измерения сигналами (их шлёт run/run.py):
//
//	SIGUSR1 — начало окна: базовый снимок метрик, сброс счётчиков, старт трассы;
//	SIGTERM — конец окна: итог в <out>/final.json и выход.
package main

import (
	"bufio"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math/rand"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"runtime/trace"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"gosched-bench/internal/dist"
	"gosched-bench/internal/hist"
	"gosched-bench/internal/proto"
	"gosched-bench/internal/rtm"
	"gosched-bench/internal/spin"
)

var (
	addr      = flag.String("addr", ":7070", "адрес для LC-запросов")
	lcSource  = flag.String("lc-source", "net", "net — запросы по сети; inproc — генератор внутри процесса (абляция H3)")
	lcRate    = flag.Float64("lc-rate", 0, "inproc: интенсивность LC, запросов/с")
	lcDist    = flag.String("lc-dist", "exp:50us", "inproc: распределение времени обслуживания LC")
	lcAlloc   = flag.Int("lc-alloc", 0, "inproc: байт мусора на LC-запрос")
	beUtil    = flag.Float64("be-util", 0, "BE-нагрузка как доля от GOMAXPROCS (ρ_BE)")
	beQ       = flag.Duration("be-q", time.Millisecond, "длина одной BE-задачи на CPU (гранулярность q)")
	beYield   = flag.Duration("be-yield", 0, "если >0: runtime.Gosched() внутри BE каждые столько (абляция H1)")
	beAlloc   = flag.Int("be-alloc", 0, "байт мусора на одну BE-задачу (фактор a_BE)")
	outDir    = flag.String("out", ".", "каталог для metrics.csv, final.json, trace.out")
	interval  = flag.Duration("metrics-interval", time.Second, "период строки в metrics.csv")
	traceDur  = flag.Duration("trace", 0, "если >0: после SIGUSR1 писать runtime/trace столько времени")
	calibTime = flag.Duration("calib", 200*time.Millisecond, "длительность одного замера калибровки spin")
	seed      = flag.Int64("seed", 1, "seed генераторов внутри процесса")
)

// sink удерживает аллокации, чтобы компилятор их не убрал.
var sink atomic.Pointer[[]byte]

func allocGarbage(n int) {
	if n > 0 {
		b := make([]byte, n)
		sink.Store(&b)
	}
}

// shardedHist — гистограмма с несколькими мьютексами, чтобы запись из
// обработчиков не превращалась в общую точку синхронизации.
type shardedHist struct {
	next   atomic.Uint64
	mu     [16]sync.Mutex
	shards [16]*hist.Hist
}

func newSharded() *shardedHist {
	s := &shardedHist{}
	for i := range s.shards {
		s.shards[i] = hist.New()
	}
	return s
}

func (s *shardedHist) Record(v int64) {
	i := s.next.Add(1) % 16
	s.mu[i].Lock()
	s.shards[i].Record(v)
	s.mu[i].Unlock()
}

func (s *shardedHist) Reset() {
	for i := range s.shards {
		s.mu[i].Lock()
		s.shards[i] = hist.New()
		s.mu[i].Unlock()
	}
}

func (s *shardedHist) Merged() *hist.Hist {
	h := hist.New()
	for i := range s.shards {
		s.mu[i].Lock()
		h.Merge(s.shards[i])
		s.mu[i].Unlock()
	}
	return h
}

// Счётчики окна измерения.
var (
	beStarted atomic.Int64
	beDone    atomic.Int64
	lcWait    = newSharded() // TStart - TRead (net) или TStart - запланированный момент (inproc)
	lcTotal   = newSharded() // inproc: полная задержка от запланированного момента
)

// --- BE ---

func beJob(q, yield time.Duration, alloc int) {
	allocGarbage(alloc)
	if yield <= 0 {
		spin.For(q)
	} else {
		for left := q; left > 0; left -= yield {
			spin.For(min(yield, left))
			runtime.Gosched()
		}
	}
	beDone.Add(1)
}

// beGenerator запускает BE-задачи пуассоновским потоком с λ = util·P/q.
// Каждая задача — новая горутина (как go f() в реальном коде).
func beGenerator(util float64, q, yield time.Duration, alloc int, r *rand.Rand) {
	p := runtime.GOMAXPROCS(0)
	rate := util * float64(p) / q.Seconds()
	log.Printf("BE: util=%.3f P=%d q=%v -> %.1f задач/с", util, p, q, rate)
	next := time.Now()
	for {
		next = next.Add(time.Duration(r.ExpFloat64() / rate * float64(time.Second)))
		if d := time.Until(next); d > 0 {
			time.Sleep(d)
		}
		beStarted.Add(1)
		go beJob(q, yield, alloc)
	}
}

// --- LC по сети ---

func handle(c net.Conn, wmu *sync.Mutex, req proto.Request, tRead int64) {
	tStart := time.Now().UnixNano()
	if trace.IsEnabled() {
		_, task := trace.NewTask(context.Background(), "lc")
		defer task.End()
	}
	allocGarbage(int(req.AllocBytes))
	spin.For(time.Duration(req.ServiceNs))
	resp := proto.Response{ID: req.ID, TRead: tRead, TStart: tStart, TEnd: time.Now().UnixNano()}
	lcWait.Record(tStart - tRead)
	var buf [proto.RespSize]byte
	resp.Encode(buf[:])
	wmu.Lock()
	_, err := c.Write(buf[:])
	wmu.Unlock()
	if err != nil {
		c.Close()
	}
}

func serveConn(c net.Conn) {
	defer c.Close()
	br := bufio.NewReaderSize(c, 64<<10)
	var wmu sync.Mutex
	var buf [proto.ReqSize]byte
	var req proto.Request
	for {
		if err := proto.ReadRequest(br, buf[:], &req); err != nil {
			return
		}
		// Отдельная горутина на запрос, как в gRPC: нет блокировки
		// «в голове очереди» внутри одного соединения.
		go handle(c, &wmu, req, time.Now().UnixNano())
	}
}

func listen(ln net.Listener) {
	for {
		c, err := ln.Accept()
		if err != nil {
			log.Printf("accept: %v", err)
			return
		}
		go serveConn(c)
	}
}

// --- LC изнутри процесса (абляция H3: без netpoller) ---

func inprocGenerator(rate float64, d dist.Dist, alloc int, r *rand.Rand) {
	log.Printf("LC inproc: %.1f запросов/с, dist=%s", rate, *lcDist)
	next := time.Now()
	for {
		next = next.Add(time.Duration(r.ExpFloat64() / rate * float64(time.Second)))
		if w := time.Until(next); w > 0 {
			time.Sleep(w)
		}
		sched := next
		svc := d.Sample(r)
		go func() {
			tStart := time.Now()
			allocGarbage(alloc)
			spin.For(svc)
			lcWait.Record(tStart.Sub(sched).Nanoseconds())
			lcTotal.Record(time.Since(sched).Nanoseconds())
		}()
	}
}

// --- итог ---

type final struct {
	GoVersion  string    `json:"go_version"`
	GOMAXPROCS int       `json:"gomaxprocs"`
	ItersPerUs float64   `json:"spin_iters_per_us"`
	Args       []string  `json:"args"`
	Window     rtm.Delta `json:"window"`
	BE         struct {
		OfferedUtil  float64 `json:"offered_util"`
		JobsStarted  int64   `json:"jobs_started"`
		JobsDone     int64   `json:"jobs_done"`
		AchievedUtil float64 `json:"achieved_util"` // завершённые задачи · q / (окно · P)
	} `json:"be"`
	LCWait  *hist.Hist `json:"lc_handler_wait"`           // net: TStart-TRead; inproc: TStart-sched
	LCTotal *hist.Hist `json:"lc_inproc_total,omitempty"` // только inproc
}

func writeJSON(path string, v any) {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		log.Fatalf("json: %v", err)
	}
	if err := os.WriteFile(path, b, 0o644); err != nil {
		log.Fatalf("write %s: %v", path, err)
	}
}

func main() {
	flag.Parse()
	log.SetFlags(log.Lmicroseconds)
	if err := os.MkdirAll(*outDir, 0o755); err != nil {
		log.Fatal(err)
	}

	ipu := spin.Calibrate(*calibTime, 5)
	log.Printf("go=%s GOMAXPROCS=%d spin=%.2f итераций/мкс", runtime.Version(), runtime.GOMAXPROCS(0), ipu)

	mf, err := os.Create(filepath.Join(*outDir, "metrics.csv"))
	if err != nil {
		log.Fatal(err)
	}
	stopSampler := make(chan struct{})
	go rtm.Sampler(mf, *interval, stopSampler)

	r := rand.New(rand.NewSource(*seed))
	switch *lcSource {
	case "net":
		ln, err := net.Listen("tcp", *addr)
		if err != nil {
			log.Fatal(err)
		}
		go listen(ln)
	case "inproc":
		if *lcRate <= 0 {
			log.Fatal("inproc: нужен -lc-rate > 0")
		}
		d, err := dist.Parse(*lcDist)
		if err != nil {
			log.Fatal(err)
		}
		go inprocGenerator(*lcRate, d, *lcAlloc, rand.New(rand.NewSource(*seed+1)))
	default:
		log.Fatalf("неизвестный -lc-source %q", *lcSource)
	}
	if *beUtil > 0 {
		go beGenerator(*beUtil, *beQ, *beYield, *beAlloc, r)
	}

	sig := make(chan os.Signal, 4)
	signal.Notify(sig, syscall.SIGUSR1, syscall.SIGTERM, syscall.SIGINT)
	fmt.Println("READY") // run.py ждёт эту строку
	base := rtm.Take()
	var beBase int64
	for s := range sig {
		switch s {
		case syscall.SIGUSR1:
			base = rtm.Take()
			beBase = beDone.Load()
			beStarted.Store(0)
			lcWait.Reset()
			lcTotal.Reset()
			log.Printf("окно измерения: старт")
			if *traceDur > 0 {
				startTrace(filepath.Join(*outDir, "trace.out"), *traceDur)
			}
		default:
			end := rtm.Take()
			var f final
			f.GoVersion = runtime.Version()
			f.GOMAXPROCS = runtime.GOMAXPROCS(0)
			f.ItersPerUs = spin.ItersPerUs()
			f.Args = os.Args[1:]
			f.Window = rtm.Diff(base, end)
			f.BE.OfferedUtil = *beUtil
			f.BE.JobsStarted = beStarted.Load()
			f.BE.JobsDone = beDone.Load() - beBase
			if f.Window.WindowS > 0 {
				f.BE.AchievedUtil = float64(f.BE.JobsDone) * beQ.Seconds() / (f.Window.WindowS * float64(f.GOMAXPROCS))
			}
			f.LCWait = lcWait.Merged()
			if *lcSource == "inproc" {
				f.LCTotal = lcTotal.Merged()
			}
			close(stopSampler)
			writeJSON(filepath.Join(*outDir, "final.json"), &f)
			mf.Close()
			log.Printf("окно измерения: конец, %.1f с; итог в final.json", f.Window.WindowS)
			return
		}
	}
}

func startTrace(path string, d time.Duration) {
	f, err := os.Create(path)
	if err != nil {
		log.Printf("trace: %v", err)
		return
	}
	if err := trace.Start(f); err != nil {
		log.Printf("trace: %v", err)
		f.Close()
		return
	}
	time.AfterFunc(d, func() {
		trace.Stop()
		f.Close()
		log.Printf("trace: записано %v в %s", d, path)
	})
}
