"""Puzzle solver built on the gds2v core library.

Modules (each runnable as ``python -m puzzle.<name>``):

    analyze     recover the design's structure (FSM, counters, region map)
    solve       solve the Star Battle, verify on the netlist, write solution.vcd
    visualize   floorplan / region / logo figures
    vcdtool     read and write the puzzle's VCD traces

All Verilog is produced automatically by the gds2v tool (structural, behavioural and
lifted RTL); this package adds the puzzle-specific analysis on top of it.
"""
