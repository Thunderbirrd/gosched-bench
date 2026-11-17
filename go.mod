module gosched-bench

go 1.26

// Эксперименты статьи 1 идут на фиксированной версии Go.
// При go < 1.27.3 локально Go сам скачает этот toolchain (GOTOOLCHAIN=auto).
toolchain go1.27.3
