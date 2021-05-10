# Auto-generated by Xeda

create_project -part {{flow.fpga_part}} -force -verbose {{design.name}}

# These settings are set by Xeda
set design_name           {{design.name}}
set vhdl_std              {{design.language.vhdl.standard}}
set nthreads              {{nthreads}}

set fail_critical_warning {{flow.fail_critical_warning}}
set fail_timing           {{flow.fail_timing}}
set bitstream             false

set reports_dir           {{reports_dir}}
set synth_output_dir      {{synth_output_dir}}
set checkpoints_dir       {{checkpoints_dir}}

{% include 'util.tcl' %}

puts "Targeting device: {{flow.fpga_part}}"

set_param general.maxThreads ${nthreads}

file mkdir ${synth_output_dir}
file mkdir ${reports_dir}
file mkdir [file join ${reports_dir} post_synth]
file mkdir [file join ${reports_dir} post_place]
file mkdir [file join ${reports_dir} post_route]
file mkdir ${checkpoints_dir}

puts "\n================================( Read Design Files and Constraints )================================"

{% for src in design.rtl.sources %}
{%- if src.type == 'verilog' %}
{%- if src.variant == 'systemverilog' %}
puts "Reading SystemVerilog file {{src.file}}"
if { [catch {eval read_verilog -sv {{src.file}} } myError]} {
    errorExit $myError
}
{% else %}
puts "Reading Verilog file {{src.file}}"
if { [catch {eval read_verilog {{src.file}} } myError]} {
    errorExit $myError
}
{%- endif %}
{%- endif %}
{% if src.type == 'vhdl' %}
puts "Reading VHDL file {{src.file}}"
if { [catch {eval read_vhdl {% if design.language.vhdl.standard == "08" %} -vhdl2008 {%- endif %} {{src.file}} } myError]} {
    errorExit $myError
}
{%- endif %}
{%- endfor %}

# TODO: Skip saving some artifects in case timing not met or synthesis failed for any reason

{% for xdc_file in xdc_files %}
read_xdc {{xdc_file}}
{% endfor %}

set_property top {{design.rtl.top}} [get_fileset sources_1]


{% for name,value in options.synth.items() %}
set_property -name {{name}} -value {{value}} -objects [get_runs synth_1]
{% endfor %}

{% for name,value in options.impl.items() %}
set_property -name {{name}} -value {{value}} -objects [get_runs impl_1]
{% endfor %}

puts "\n================================( Running Synthesis )================================="
launch_runs synth_1 -jobs {{nthreads}}
wait_on_run synth_1

puts "\n================================( Running Implementation )================================="
launch_runs impl_1 -jobs {{nthreads}}
wait_on_run impl_1


puts "\n=============================( Opening Implemented Design )=============================="
open_run impl_1


puts "\n=============================( Writing Checkpoint )=============================="
write_checkpoint -force ${checkpoints_dir}/post_route

puts "\n==============================( Writing Reports )================================"
report_timing_summary -check_timing_verbose -no_header -report_unconstrained -path_type full -input_pins -max_paths 10 -delay_type min_max -file ${reports_dir}/post_route/timing_summary.rpt
report_timing  -no_header -input_pins  -unique_pins -sort_by group -max_paths 100 -path_type full -delay_type min_max -file ${reports_dir}/post_route/timing.rpt
reportCriticalPaths ${reports_dir}/post_route/critpath_report.csv
## report_clock_utilization                                        -force -file ${reports_dir}/post_route/clock_utilization.rpt
report_utilization                                              -force -file ${reports_dir}/post_route/utilization.rpt
report_utilization                                              -force -file ${reports_dir}/post_route/utilization.xml -format xml
report_utilization -hierarchical                                -force -file ${reports_dir}/post_route/hierarchical_utilization.xml -format xml
report_utilization -hierarchical                                -force -file ${reports_dir}/post_route/hierarchical_utilization.rpt
## report_utilization -hierarchical                                -force -file ${reports_dir}/post_route/hierarchical_utilization.xml -format xml
report_power                                                    -file ${reports_dir}/post_route/power.rpt
report_drc                                                      -file ${reports_dir}/post_route/drc.rpt
## report_ram_utilization                                          -file ${reports_dir}/post_route/ram_utilization.rpt -append
report_methodology                                              -file ${reports_dir}/post_route/methodology.rpt

set timing_slack [get_property SLACK [get_timing_paths]]
puts "Final timing slack: $timing_slack ns"

report_qor_suggestions -file ${reports_dir}/post_route/qor_suggestions.rpt 
# -max_strategies 5
# write_qor_suggestions -force qor_suggestions.rqs

# close_project

if {$timing_slack < 0} {
    puts "\n===========================( *ENABLE ECHO* )==========================="
    puts "ERROR: Failed to meet timing by $timing_slack, see [file join ${reports_dir} post_route timing_summary.rpt] for details"
    if {$fail_timing} {
        exit 1
    }
    puts "\n===========================( *DISABLE ECHO* )==========================="
} else {
    puts "\n==========================( Writing Netlist and SDF )============================="
    write_sdf -mode timesim -process_corner slow -force -file ${synth_output_dir}/impl_timesim.sdf
    # should match sdf
    write_verilog -mode timesim -sdf_anno false -force -file ${synth_output_dir}/impl_timesim.v
##    write_verilog -mode timesim -sdf_anno false -include_xilinx_libs -write_all_overrides -force -file ${synth_output_dir}/impl_timesim_inlined.v
##    write_verilog -mode funcsim -force ${synth_output_dir}/impl_funcsim_noxlib.v
##    write_vhdl    -mode funcsim -include_xilinx_libs -write_all_overrides -force -file ${synth_output_dir}/impl_funcsim.vhd
    write_xdc -no_fixed_only -force ${synth_output_dir}/impl.xdc

    if {${bitstream}} {
        puts "\n==============================( Writing Bitstream )==============================="
        write_bitstream -force ${synth_output_dir}/bitstream.bit
    }
    showWarningsAndErrors
}



