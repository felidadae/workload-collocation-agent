import os
import statistics
import requests
import datetime
from typing import Dict, List, Tuple, Optional, Union, Iterable, Any
from collections import defaultdict
from shutil import copyfile
import pandas as pd
from urllib import parse
from dataclasses import dataclass, field
from enum import Enum
import pprint

from runner import ClusterInfoLoader
import logging

FORMAT = "%(asctime)-15s:%(levelname)s %(module)s %(message)s"
logging.basicConfig(format=FORMAT, level=logging.DEBUG)
logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


class ExperimentType(Enum):
    SingleWorkloadsRun = 'SingleWorkloadsRun'
    SteppingSingleWorkloadsRun = 'SteppingSingleWorkloadsRun'
    ThreeStageStandardRun = 'ThreeStageStandardRun'

WINDOW_LENGTH = 60 * 5

@dataclass
class ExperimentMeta:
    data_path: str
    title: str
    description: str
    params: Dict[str, str]
    changelog: str
    bugs: str
    experiment_type: ExperimentType = ExperimentType.ThreeStageStandardRun
    experiment_baseline_index: int = 0
    commit_hash: str = 'unknown'

    def data_path_(self):
        return os.path.basename(self.data_path)

    # def __str__(self):
    #     return pprint.pformat(experiment_meta, indent=4, width=40).replace(',', '\n').replace('(', '(\n ')


class PrometheusClient:
    BASE_URL = "http://100.64.176.36:30900"

    @staticmethod
    def instant_query(query, time):
        """ instant query
        https://prometheus.io/docs/prometheus/latest/querying/api/#instant-vectors

        Sample usage:
        r = instant_query("avg_over_time(task_llc_occupancy_bytes
            {app='redis-memtier-big', host='node37', task_name='default/redis-memtier-big-0'}[3000s])",
            1583395200)
        """
        urli = PrometheusClient.BASE_URL + '/api/v1/query?{}'.format(parse.urlencode(dict(
            query=query, time=time,)))
        r = requests.get(urli)
        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            raise Exception(r.content) from e
        j = r.json()
        assert j['status'] != 'error'
        assert j['data']['resultType'] == 'vector'
        data = j['data']['result']
        return data

    @staticmethod
    def convert_result_to_dict(result):
        """ Very memory inefficient!"""
        d = defaultdict(list)
        for serie in result:
            metric = serie['metric']
            # instant query
            if 'value' in serie:
                for label_name, label_value in metric.items():
                    d[label_name].append(label_value)
                timestamp, value = serie['value']
                d['value'].append(value)
                d['timestamp'].append(pd.Timestamp(timestamp, unit='s'))
            # range query
            elif 'values' in serie:
                for value in serie['values']:
                    for label_name, label_value in metric.items():
                        d[label_name].append(label_value)
                    timestamp, value = value
                    d['value'].append(value)
                    d['timestamp'].append(pd.Timestamp(timestamp, unit='s'))
            else:
                raise Exception('unsupported result type! (only matrix and instant are supported!)')
        d = dict(d)
        return d


@dataclass
class Stage:
    def __init__(self, t_start: int, t_end: int):
        SAFE_DELTA = 60  # 60 seconds back
        t_end -= SAFE_DELTA

        self.tasks: List[Task] = AnalyzerQueries.query_tasks_list(t_end)
        AnalyzerQueries.query_task_performance_metrics(time=t_end, tasks=self.tasks)

        self.nodes: Dict[str, Node] = AnalyzerQueries.query_nodes_list(t_end)
        AnalyzerQueries.query_platform_performance_metrics(time=t_end, nodes=self.nodes)


@dataclass
class Task:
    name: str
    workload_name: str
    node: str
    performance_metrics: Dict[str, float] = field(default_factory=lambda: {})

    def if_aep(self):
        return self.node in ClusterInfoLoader.get_instance().get_aep_nodes()

    def get_throughput(self, subvalue) -> Optional[float]:
        if Metric.TASK_THROUGHPUT in self.performance_metrics:
            return float(self.performance_metrics[Metric.TASK_THROUGHPUT][subvalue])
        else:
            return None

    def get_latency(self, subvalue) -> Optional[float]:
        if Metric.TASK_LATENCY in self.performance_metrics:
            return float(self.performance_metrics[Metric.TASK_LATENCY][subvalue])
        else:
            return None


@dataclass
class Node:
    name: str
    performance_metrics: Dict[str, float] = field(default_factory=lambda: {})

    def to_dict(self, nodes_capacities: Dict[str, Dict]) -> Dict:
        # @TODO should be taken from queries
        node_cpu = nodes_capacities[self.name]['cpu']
        node_mem = nodes_capacities[self.name]['mem']

        if Metric.PLATFORM_CPU_REQUESTED not in self.performance_metrics:
            # means that no tasks were run on the node
            return {}

        return {
            'name': self.name, 
            'cpu_requested': round(float(self.performance_metrics[Metric.PLATFORM_CPU_REQUESTED]['instant']), 2),
            'cpu_requested [%]': round(float(self.performance_metrics[Metric.PLATFORM_CPU_REQUESTED]['instant'])/node_cpu*100, 2),
            'cpu_util': round(float(self.performance_metrics[Metric.PLATFORM_CPU_UTIL]['instant']),2),
            'mem_requested': round(float(self.performance_metrics[Metric.PLATFORM_MEM_USAGE]['instant']),2),
            'mem_requested [%]': round(float(self.performance_metrics[Metric.PLATFORM_MEM_USAGE]['instant'])/node_mem*100,2),
            'mbw_total':     round(float(self.performance_metrics[Metric.PLATFORM_MBW_TOTAL]['instant']),2),
            'dram_hit_ratio [%]': round(float(self.performance_metrics[Metric.PLATFORM_DRAM_HIT_RATIO]['instant']) * 100,2),
            'wss_used (aprox)': round(float(self.performance_metrics[Metric.PLATFORM_WSS_USED]['instant']),2),
            'wss_used (aprox) [%]': round(float(self.performance_metrics[Metric.PLATFORM_WSS_USED]['instant'])/193*100,2),
            'mem/cpu': round(float(self.performance_metrics[Metric.PLATFORM_MEM_USAGE]['instant'])/
                       float(self.performance_metrics[Metric.PLATFORM_CPU_REQUESTED]['instant']), 2)
        }

    @staticmethod
    def to_dataframe(nodes: List[Any], nodes_capacities: Dict[str, Dict]) -> pd.DataFrame:
        return pd.DataFrame([node.to_dict(nodes_capacities) for node in nodes])


@dataclass
class Stat:
    """Statistics"""
    _avg: float
    _min: float
    _max: float
    _stdev: float


@dataclass
class WStat:
    """Workload Statistics"""
    name: str
    latency: Stat
    throughput: Stat
    count: int

    def to_dict(self):
        return {
            "LB_min": round(self.latency._min, 2),
            "LB_avg": round(self.latency._avg, 2),
            "LB_max": round(self.latency._max, 2),
            "L_stdev":  round(self.latency._stdev, 2),
            "L_stdev[%]":  round(self.latency._stdev /self.latency._avg * 100, 2),
            # ---
            "TB_min": round(self.throughput._min, 2),
            "TB_avg": round(self.throughput._avg, 2),
            "TB_max": round(self.throughput._max, 2),
            "T_stdev":  round(self.throughput._stdev, 2),
            "T_stdev[%]":  round(self.throughput._stdev / self.throughput._avg * 100, 2),
            # ---
            "B_count": self.count,
            "app": self.name
        }

    @staticmethod
    def to_dataframe(wstats: List[Any]) -> pd.DataFrame:
        return pd.DataFrame([wstat.to_dict() for wstat in wstats])


def calculate_task_summaries(tasks: List[Task], workloads_baseline: Dict[str, WStat]) -> List[Dict[str, Union[float, str]]]:
    """
    Calculate summary for each task defined in >>tasks<< as comparison to workloads_baseline.
    @TODO what if there is no given task workload in workloads_baseline ?
    """
    tasks_summaries = []
    for task in tasks:
        workload = task.workload_name

        # avg of a task behaviour
        throughput = task.get_throughput('avg')
        latency = task.get_latency('avg')
        if throughput is None or latency is None:
            logging.debug('Ignoring task {} cause not available'.format(task))
            continue
        assert throughput is not None
        assert latency is not None

        task_summary = {
            "L": latency,
            "L[avg][{}s]".format(WINDOW_LENGTH): latency,
            "L[q0.1][{}s]".format(WINDOW_LENGTH): task.get_latency('q0.1,'),
            "L[q0.9][{}s]".format(WINDOW_LENGTH): task.get_latency('q0.9,'),
            "L[stdev][{}s]".format(WINDOW_LENGTH): task.get_latency('stdev'),
            "L[stdev][{}s][%]".format(WINDOW_LENGTH): -1 if task.get_latency('avg') == 0 else task.get_latency('stdev')/task.get_latency('avg') * 100,
            # "LB_min": workloads_baseline[workload].latency._min,
            # "LB_avg": workloads_baseline[workload].latency._avg,
            # "LB_max": workloads_baseline[workload].latency._max,
            # ----
            "T": throughput,
            "T[avg][{}s]".format(WINDOW_LENGTH): throughput,
            "T[q0.9][{}s]".format(WINDOW_LENGTH): task.get_throughput('q0.9,'),
            "T[q0.1][{}s]".format(WINDOW_LENGTH): task.get_throughput('q0.1,'),
            "T[stdev][{}s]".format(WINDOW_LENGTH): task.get_throughput('stdev'),

            "T[stdev][{}s][%]".format(WINDOW_LENGTH): -1 if task.get_throughput('avg') == 0 else task.get_throughput('stdev')/task.get_throughput('avg') * 100,
            # "TB_min": workloads_baseline[workload].throughput._min,
            # "TB_avg": workloads_baseline[workload].throughput._avg,
            # "TB_max": workloads_baseline[workload].throughput._max,
            # "B_count": workloads_baseline[workload].count,
            # ----
            # "L_nice[%]": latency / workloads_baseline[workload].latency._max * 100,
            # "T_nice[%]": throughput / workloads_baseline[workload].throughput._min * 100,
            # # ----
            # "L_avg[%]": latency / workloads_baseline[workload].latency._avg * 100,
            # "T_avg[%]": throughput / workloads_baseline[workload].throughput._avg * 100,
            # # ----
            # "L_strict[%]": latency / workloads_baseline[workload].latency._min * 100,
            # "T_strict[%]": throughput / workloads_baseline[workload].throughput._max * 100,
            # ----
            "task": task.name, "app": task.workload_name, "node": task.node
        }
        # for key, val in task_summary.items():
        #     if '{window_length}' in key:
        #         task_summary[key.format(window_length=WINDOW_LENGTH)] = val
        #         del task_summary[key]
        for key, val in task_summary.items():
            if type(val) == float:
                task_summary[key] = round(val, 3)

        ts = task_summary
        # ts["pass_nice"] = ts['T_nice[%]'] > 80 and ts['L_nice[%]'] < 150
        # ts["pass_avg"] = ts['T_avg[%]'] > 80 and ts['L_avg[%]'] < 150
        # ts["pass_strict"] = ts['T_strict[%]'] > 80 and ts['L_strict[%]'] < 150

        tasks_summaries.append(ts)
    return tasks_summaries


class StagesAnalyzer:
    def __init__(self, events, workloads):
        self.events_data = (events, workloads)
        assert len(self.events_data[0]) % 2 == 0
        self.stages_count = int(len(self.events_data[0]) / 2)

        # @Move to loader
        T_DELTA = os.environ.get('T_DELTA', 0)

        self.stages = [] 
        for i in range(self.stages_count):
            self.stages.append(Stage(t_start=events[i*2][0].timestamp()+T_DELTA, t_end=events[i*2+1][0].timestamp() + T_DELTA))

    def delete_report_files(self, report_root_dir):
        if os.path.isdir(report_root_dir):
            for file_ in os.listdir(report_root_dir):
                os.remove(os.path.join(report_root_dir, file_))

    def get_all_tasks_count_in_stage(self, stage: int) -> int:
        """Only returns tasks count, directly from metric."""
        return sum(int(node.performance_metrics[Metric.POD_SCHEDULED]['instant'])
                   for node in self.stages[stage].nodes.values()
                   if Metric.POD_SCHEDULED in node.performance_metrics)

    def get_all_workloads_in_stage(self, stage_index: int):
        return set(task.workload_name for task in self.stages[stage_index].tasks.values())

    def get_all_tasks_in_stage_on_nodes(self, stage_index: int, nodes: List[str]):
        return [task for task in self.stages[stage_index].tasks.values() if task.node in nodes]

    def get_all_nodes_in_stage(self, stage_index: int) -> List[str]:
        return [nodename for nodename in self.stages[stage_index].nodes]

    def calculate_per_workload_wstats_per_stage(self, workloads: Iterable[str], stage_index) -> Dict[str, WStat]:
        """Calculate Wstats for all workloads in list for stage (stage_index). Takes data from all nodes."""
        workloads_wstats: Dict[str, WStat] = {}
        for workload in workloads:
            tasks = [task for task in self.stages[stage_index].tasks.values() if task.workload_name == workload]
            tasks = [task for task in tasks if task.node != 'node101']

            # avg but from 12 sec for a single task
            throughputs_list = [task.get_throughput('avg') for task in tasks if task.get_throughput('avg') is not None]
            latencies_list =  [task.get_latency('avg') for task in tasks if task.get_latency('avg') is not None]

            t_max, t_min, t_avg, t_stdev = max(throughputs_list), min(throughputs_list), statistics.mean(throughputs_list), statistics.stdev(throughputs_list)
            l_max, l_min, l_avg, l_stdev = max(latencies_list), min(latencies_list), statistics.mean(latencies_list), statistics.stdev(latencies_list)

            workloads_wstats[workload] = WStat(latency=Stat(l_avg, l_min, l_max, l_stdev), throughput=Stat(t_avg, t_min, t_max, t_stdev), count=len(tasks), name=workload)
        return workloads_wstats

    def get_stages_count(self):
        return self.stages_count

    def aep_report(self, experiment_meta: ExperimentMeta, experiment_index: int):
        """
        Compare results from AEP to DRAM:
        1) list all workloads which are run on AEP (Task.workload.name) in stage 3 (or 2)
          a) for all this workloads read performance on DRAM in stage 1
        2) for assertion and consistency we could also check how compare results in all stages
        3) compare results which we got AEP vs DRAM seperately for stage 2 and 3
          a) for each workload:
        """
        # baseline results in stage0 on DRAM
        for i in range(len(self.stages)):
            assert self.get_all_tasks_count_in_stage(0) > 5

        workloads_wstats: List[Dict[str, Wstat]] = []
        tasks_summaries__per_stage: List[List[Dict]] = []
        node_summaries__per_stage: List[List[Dict]] = []
        workloads_baseline: Dict[str, WStat] = None

        for stage_index in range(0, self.get_stages_count()):
            workloads_wstat = self.calculate_per_workload_wstats_per_stage(self.get_all_workloads_in_stage(stage_index), stage_index=stage_index)
            workloads_wstats.append(workloads_wstat)
        workloads_baseline = workloads_wstats[experiment_meta.experiment_baseline_index]
        for stage_index in range(0, self.get_stages_count()):
            tasks = self.get_all_tasks_in_stage_on_nodes(stage_index=stage_index, nodes=self.get_all_nodes_in_stage(stage_index))
            # ---
            tasks_summaries = calculate_task_summaries(tasks, workloads_baseline)
            tasks_summaries__per_stage.append(tasks_summaries)
            # ---
            nodes_capacities = ClusterInfoLoader.get_instance().get_nodes()
            nodes_summaries = [self.stages[stage_index].nodes[node].to_dict(nodes_capacities) for node in self.get_all_nodes_in_stage(stage_index)]
            nodes_summaries = [s for s in nodes_summaries if list(s.keys())]
            node_summaries__per_stage.append(nodes_summaries)

        # Transform to DataFrames keeping the same names
        workloads_wstats: List[DataFrame] = [WStat.to_dataframe(el.values()) for el in workloads_wstats]
        tasks_summaries__per_stage: List[pd.DataFrame] = [pd.DataFrame(el) for el in tasks_summaries__per_stage]
        node_summaries__per_stage: List[pd.DataFrame] = [pd.DataFrame(el) for el in node_summaries__per_stage]

        exporter = TxtStagesExporter(
            events_data=self.events_data,
            experiment_meta=experiment_meta,
            experiment_index=experiment_index,
            # ---
            export_file_path=os.path.join(experiment_meta.data_path, 'runner_analyzer', 'results.txt'),
            utilization_file_path=os.path.join(experiment_meta.data_path, 'choosen_workloads_utilization.{}.txt'.format(experiment_index)),
            # ---
            workloads_summaries=workloads_wstats,
            tasks_summaries=tasks_summaries__per_stage,
            node_summaries=node_summaries__per_stage)

        if experiment_meta.experiment_type == ExperimentType.ThreeStageStandardRun:
            exporter.export_to_txt()
        elif experiment_meta.experiment_type == ExperimentType.SingleWorkloadsRun:
            exporter.export_to_txt_single()
        elif experiment_meta.experiment_type == ExperimentType.SteppingSingleWorkloadsRun:
            exporter.export_to_txt_stepping_single()
        else:
            raise Exception('Unsupported experiment type!')


@dataclass
class TxtStagesExporter:
    events_data: List
    experiment_meta: ExperimentMeta
    experiment_index: int
    export_file_path: str
    utilization_file_path: str

    workloads_summaries: List[pd.DataFrame]
    tasks_summaries: List[pd.DataFrame]
    node_summaries: List[pd.DataFrame]

    def __post_init__(self):
        # @TODO remove
        self.limits = {'L[%]': 150, 'T[%]': 80}

    def export_to_txt_single(self):
        logging.debug("Saving results to {}".format(self.export_file_path))

        runner_analyzer_results_dir = os.path.join(self.experiment_meta.data_path, 'runner_analyzer')
        if not os.path.isdir(runner_analyzer_results_dir):
            os.mkdir(runner_analyzer_results_dir)

        with open(os.path.join(runner_analyzer_results_dir, 'results.txt'), 'a+') as fref:
            self._fref = fref

            self._seperator()
            self._metadata()
            self._baseline(stage_index=1)
            self._tasks_summaries_in_stage(stage_index=1, title='Task summaries for all tasks')
            self._seperator(ending=True)

            self._fref = None

    def export_to_txt_stepping_single(self):
        logging.debug("Saving results to {}".format(self.export_file_path))

        runner_analyzer_results_dir = os.path.join(self.experiment_meta.data_path, 'runner_analyzer')
        if not os.path.isdir(runner_analyzer_results_dir):
            os.mkdir(runner_analyzer_results_dir)

        with open(os.path.join(runner_analyzer_results_dir, 'results.txt'), 'a+') as fref:
            self._fref = fref
            self._seperator()
            self._metadata()
            self._seperator(ending=True)
            for stage_index in range(len(self.workloads_summaries)):
                self._seperator()
                self._baseline(title="DRAM workload summary", stage_index=stage_index)
                self._tasks_summaries_in_stage(stage_index=stage_index, title='Task summaries for PMEM:node101', filter_nodes=['node101'])
                self._tasks_summaries_in_stage(stage_index=stage_index, title='Task summaries for DRAM:node103', filter_nodes=['node103'])
                self._node_summaries_in_stage(stage_index=stage_index, title='Node summaries for DRAM:node103', filter_nodes=['node103'])
                self._seperator(ending=True)
            self._fref = None

    def _tasks_summaries_in_stage(self, title: str, stage_index:int, filter_nodes: Optional[List[str]] = None):
        df = self.tasks_summaries[stage_index]  # df == dataframe
        if filter_nodes is not None:
            df = df[df.node.isin(filter_nodes)]

        self._fref.write('\n{}\n'.format(title))
        self._fref.write(df.to_string())
        self._fref.write('\n')

    def _node_summaries_in_stage(self, title: str, stage_index: int, filter_nodes = Optional[List[str]]):
        df = self.node_summaries[stage_index]  # df == dataframe
        if filter_nodes is not None:
            df = df[df.name.isin(filter_nodes)]

        self._fref.write('\n{}\n'.format(title))
        self._fref.write(df.to_string())
        self._fref.write('\n')

    def export_to_txt(self):
        logging.debug("Saving results to {}".format(self.export_file_path))

        runner_analyzer_results_dir = os.path.join(self.experiment_meta.data_path, 'runner_analyzer')
        if not os.path.isdir(runner_analyzer_results_dir):
            os.mkdir(runner_analyzer_results_dir)

        with open(os.path.join(runner_analyzer_results_dir, 'results.txt'), 'a+') as fref:
            self._fref = fref

            self._seperator()
            self._metadata()
            self._baseline()
            self._aep_tasks()
            self._seperator(ending=True)

            self._fref = None

    def _seperator(self, ending=False):
        self._fref.write('*' * 90 + '\n')
        if ending:
            self._fref.write('\n\n')

    def _metadata(self):
        # self._fref.write("Experiment meta: {}\n\n".format(self.experiment_meta))
        self._fref.write("Experiment index: {}\n".format(self.experiment_index))
        # for i in range(5):
        #     self._fref.write("Time event {}: {}.\n".format(i, self.events_data[0][i][0].strftime("%d-%b-%Y (%H:%M:%S)")))
        self._fref.write("Time events from: {} to: {}.\n".format(self.events_data[0][0][0].strftime("%d-%b-%Y (%H:%M:%S)"),
                                                                 self.events_data[0][-1][0].strftime("%d-%b-%Y (%H:%M:%S)")))

        workloads_list = ["({}, {})".format(workload, count) for workload, count in self.events_data[1].items()]
        workloads_list = sorted(workloads_list)
        self._fref.write("Workloads scheduled: {}\n{}".format(len(workloads_list), print_n_per_row(n=5, list_=workloads_list)))

        if os.path.isfile(self.utilization_file_path):
            utilization = open(self.utilization_file_path).readlines()[0].rstrip()
            self._fref.write("Utilization of resources: {}\n".format(utilization))
        else:
            self._fref.write("Utilization of resources: unknown\n")


    def _baseline(self, title="BASELINE", stage_index=0):
        self._fref.write("***{}(stage_index={})***\n".format(title, stage_index))
        self._fref.write(str(self.workloads_summaries[stage_index].to_string()))
        self._fref.write('\n')

    def _aep_tasks(self):
        for istage, title in (1, "KUBERNETES BASELINE"), (2, "WCA-SCHEDULER"):
            self._fref.write("\n***{}***\n".format(title))
            self._fref.write(str(self.node_summaries[istage].to_string()))
            self._fref.write('\n\n')
            self._fref.write(str(self.tasks_summaries[istage].to_string()))
            self._fref.write('\n\n')
            # ---
            self._fref.write('Passed {}/{} avg limit >>{}<<\n'.format(
                            len([val for val in self.tasks_summaries[istage]['pass_avg'] if val]),
                            len(self.tasks_summaries[istage]['pass_avg']), self.limits))
            self._fref.write('Passed {}/{} optimistic limit >>{}<<\n'.format(
                            len([val for val in self.tasks_summaries[istage]['pass_nice'] if val]),
                            len(self.tasks_summaries[istage]['pass_nice']), self.limits))
            self._fref.write('Passed {}/{} strict limit >>{}<<\n'.format(
                            len([val for val in self.tasks_summaries[istage]['pass_strict'] if val]),
                            len(self.tasks_summaries[istage]['pass_strict']), self.limits))



def print_n_per_row(n, list_):
    r = ""
    for i in range(int((len(list_)+1)/n)):
        k = i*n
        l = k+n if k+n < len(list_) else len(list_)
        r += ", ".join(list_[k:l])
        r += '\n'
    return r


class Metric(Enum):
    TASK_THROUGHPUT = 'task_throughput'
    TASK_LATENCY = 'task_latency'

    # platform
    TASK_UP = 'task_up'
    WCA_UP = 'wca_up'
    POD_SCHEDULED = 'platform_tasks_scheduled'
    PLATFORM_MEM_USAGE = 'platform_mem_usage'
    PLATFORM_CPU_REQUESTED = 'platform_cpu_requested'
    PLATFORM_CPU_UTIL = 'platform_cpu_util'
    PLATFORM_MBW_TOTAL = 'platform_mbw_total'
    PLATFORM_DRAM_HIT_RATIO = 'platform_dram_hit_ratio'
    PLATFORM_WSS_USED = 'platform_wss_used'

    
MetricsQueries = {
    Metric.TASK_THROUGHPUT: 'apm_sli2',
    Metric.TASK_LATENCY: 'apm_sli',

    # platform
    Metric.TASK_UP: 'task_up',
    Metric.WCA_UP: 'wca_up',
    Metric.POD_SCHEDULED: 'wca_tasks',
    Metric.PLATFORM_MEM_USAGE: 'sum(task_requested_mem_bytes) by (nodename) / 1e9',
    Metric.PLATFORM_CPU_REQUESTED: 'sum(task_requested_cpus) by (nodename)',
    Metric.PLATFORM_CPU_UTIL: "sum(1-rate(node_cpu_seconds_total{mode='idle'}[10s])) by(nodename) / sum(platform_topology_cpus) by (nodename)",
    Metric.PLATFORM_MBW_TOTAL: 'sum(platform_dram_reads_bytes_per_second + platform_pmm_reads_bytes_per_second) by (nodename) / 1e9',
    Metric.PLATFORM_DRAM_HIT_RATIO: 'avg(platform_dram_hit_ratio) by (nodename)',
    Metric.PLATFORM_WSS_USED: 'sum(avg_over_time(task_wss_referenced_bytes[15s])) by (nodename) / 1e9',
}


class Function(Enum):
    AVG = 'avg_over_time'
    QUANTILE = 'quantile_over_time'
    STDEV = 'stddev_over_time'


FunctionsDescription = {
    Function.AVG: 'avg',
    Function.QUANTILE: 'q',
    Function.STDEV: 'stdev',
}


def build_function_call_id(function: Function, arg: str):
    return "{}{}".format(FunctionsDescription[function], str(arg))


class AnalyzerQueries:
    """Class used for namespace"""

    @staticmethod
    def query_tasks_list(time) -> Dict[str, Task]:
        query_result = PrometheusClient.instant_query(MetricsQueries[Metric.TASK_UP], time)
        tasks = {}
        for metric in query_result:
            metric = metric['metric']
            task_name = metric['task_name']
            tasks[task_name] = Task(metric['task_name'], metric['app'],
                                    metric['nodename'])
        return tasks

    @staticmethod
    def query_nodes_list(time) -> Dict[str, Node]:
        query_result = PrometheusClient.instant_query(MetricsQueries[Metric.WCA_UP], time)
        nodes = {}
        for metric in query_result:
            metric = metric['metric']
            nodename = metric['nodename']
            nodes[nodename] = Node(nodename)
        return nodes

    @staticmethod
    def query_platform_performance_metrics(time: int, nodes: Dict[str, Node]):
        metrics = (Metric.PLATFORM_MEM_USAGE, Metric.PLATFORM_CPU_REQUESTED,
                   Metric.PLATFORM_CPU_UTIL, Metric.PLATFORM_MBW_TOTAL,
                   Metric.POD_SCHEDULED, Metric.PLATFORM_DRAM_HIT_RATIO, Metric.PLATFORM_WSS_USED)
        
        for metric in metrics:
            query_results = PrometheusClient.instant_query(MetricsQueries[metric], time)
            descr = metric.value 
            for result in query_results:
                nodename = result['metric']['nodename']
                if nodename in nodes:
                    nodes[nodename].performance_metrics[metric] = {'instant': result['value'][1]}

    @staticmethod
    def query_performance_metrics(time: int, functions_args: List[Tuple[Function, str]],
                metrics: List[Metric], window_length: int) -> Dict[str, Dict]:
        """performance metrics which needs aggregation over time"""
        query_results: Dict[Metric, Dict] = {}
        for metric in metrics:
            for function, arguments in functions_args:
                query_template = "{function}({arguments}{prom_metric}[{window_length}s])"
                query = query_template.format(function=function.value,
                                              arguments=arguments,
                                              window_length=window_length,
                                              prom_metric=MetricsQueries[metric])
                query_result = PrometheusClient.instant_query(query, time)
                aggregation_name = build_function_call_id(function, arguments)
                if metric in query_results:
                    query_results[metric][aggregation_name] = query_result
                else:
                    query_results[metric] = {aggregation_name: query_result}
        return query_results

    @staticmethod
    def query_task_performance_metrics(time: int, tasks: Dict[str, Task]):
        global WINDOW_LENGTH
        window_length = WINDOW_LENGTH  # [s]
        metrics = (Metric.TASK_THROUGHPUT, Metric.TASK_LATENCY)

        function_args = ((Function.AVG, ''), (Function.QUANTILE, '0.1,'), (Function.STDEV, ''),
                         (Function.QUANTILE, '0.9,'),)

        query_results = AnalyzerQueries.query_performance_metrics(time, function_args, metrics, window_length)

        # s_l = set([r['metric']['task_name'] for r in query_results[Metric.TASK_LATENCY]['avg']])
        # s_t = set([r['metric']['task_name'] for r in query_results[Metric.TASK_THROUGHPUT]['avg']])
        # assert s_l == s_t, "For some tasks there is not information about latency of throughput, {}".format(s_l.symmetric_difference(s_t))

        for metric, query_result in query_results.items():
            for aggregation_name, result in query_result.items():
                for per_app_result in result:
                    task_name = per_app_result['metric']['task_name']
                    value = per_app_result['value'][1]
                    if task_name not in tasks:
                        logging.debug("Ignoring task {} as not found in object tasks".format(task_name))
                        continue
                    if metric in tasks[task_name].performance_metrics:
                        tasks[task_name].performance_metrics[metric][aggregation_name] = value
                    else:
                        tasks[task_name].performance_metrics[metric] = {aggregation_name: value}


def load_events_file(filename):
    # Each python structure in seperate file.
    with open(filename) as fref:
        il = 0
        workloads_ = []
        events_ = []
        for line in fref:
            if il % 2 == 0:
                workloads = eval(line)
                if type(workloads) == dict:
                    workloads_.append(workloads)
                else:
                    break
            if il % 2 == 1:
                events = eval(line)
                if type(events) == list:
                    events_.append(events)
                else:
                    break
            il += 1
    assert len(workloads_) == len(events_), 'Wrong content of event file'
    return [(workloads, events) for workloads, events in zip(workloads_, events_)]


def analyze_3stage_experiment(experiment_meta: ExperimentMeta):
    logging.debug('Started work on {}'.format(experiment_meta.data_path))
    events_file = os.path.join(experiment_meta.data_path, 'events.txt')
    report_root_dir = os.path.join(experiment_meta.data_path, 'runner_analyzer')

    # Loads data from event file created in runner stage.
    for i, (workloads, events) in enumerate(load_events_file(events_file)):
        stages_analyzer = StagesAnalyzer(events, workloads)
        if i == 0:
            stages_analyzer.delete_report_files(report_root_dir)

        try:
            stages_analyzer.aep_report(experiment_meta, experiment_index=i)
        except Exception:
            logging.error("Skipping the whole 3stage subexperiment number {} due to exception!".format(i))
            # @TODO remove raise
            raise
            continue

        # logging.error("@TODO remove this break")
        # break


if __name__ == "__main__":
    ClusterInfoLoader.build_singleton()

    experiments_meta = [
        ExperimentMeta(
            'results/2020-03-17_creatonealg__target_score_set__ugly_fixes', 
            'making sure not PMEM app will be scheduled on DRAM',
            'the same run as previous day',
            {'target_score': -2},
            'adding hack to make sure not PMEM app will not be scheduled on DRAM', ''),

        ExperimentMeta(
            'results/2020-03-19__hp_enabled', 
            'hp enabled; the same order of running workloads',
            'first experiment with hp enabled - Score',
            {'target_score': -2},
            'enable hp; the same order of running workloads; new utilization cpu:0.25-mem:0.85', ''),

        ExperimentMeta(
            'results/2020-03-22__new_score', 
            'score2',
            'first experiment with new method of calculating score - Score2',
            {'target_score': -4},
            'score2', 'BUGS: POSSIBLE score2 was wrongly calculated, but not checked but rather not'),

        ExperimentMeta(
            'results/2020-03-24__new_score_3', 
            'score2',
            'second experiment with new method of calculating score - Score2',
            {'target_score': -3},
            'setting target_score to -3',
            'BUGS: score2 was wrongly putting dram type apps on pmem nodes'),

        ExperimentMeta(
            'results/2020-03-25__score2', 
            'score2',
            'Rerunning previous experiment',
            {'target_score': -3},
            '',
            'BUGS: score2 was wrongly putting dram type apps on pmem nodes'),

        ExperimentMeta(
            data_path='results/2020-03-26__score2', 
            title='score2',
            description='Rerunning previous experiment with fixed bugs (7 iterations only)',
            params={'target_score': -3},
            changelog='fixing SUPER IMPORTANT bug with wrongly scheduling DRAM apps',
            bugs=''),

        ExperimentMeta(
            data_path='results/2020-03-26__score2_pepe_limited', 
            title='score2 - limited workloads',
            description='Running only subset of workloads: memcached-big/big-wss, sysbench-memory-big, stress-stream-big',
            params={'target_score': -3},
            changelog='',
            bugs=''),

        ExperimentMeta(
            data_path='results/2020-03-29__each_workload_is_single', 
            title='each workload single',
            description='running each workload seperately; 15 runs;',
            params={'instances_count': 10, 'stabilize_phase_length [min]': [15, 1, 1]},
            changelog='',
            bugs=''),

        ExperimentMeta(
            data_path='results/2020-03-30__each_workload_is_single__stability_fixed_1', 
            title='each workload single',
            description='running each of 10 workloads seperately; 15 runs;',
            params={'instances_count': 'all machines', 'workloads_count': 10, 'stabilize_phase_length [min]': [35, 1, 1]},
            changelog='multiple stability fixes, frequency set to 2.1GHz, turn off node201',
            bugs='in seconds case all instances were run on single machine(dont know why)'),

        ExperimentMeta(
            data_path='results/2020-03-31__single', 
            title='each workload single',
            description='running each of workloads seperately; 35 runs;',
            params={'instances_count': 'all machines', 'workloads_count': 'all', 'stabilize_phase_length [min]': [35, 1, 1]},
            changelog='turn off node102',
            bugs='first three experiments were run not on all hosts (dont know why); frequency broken (rebooting machines)'),

        # NOTE smoke test
        # ExperimentMeta(
        #     data_path='results/2020-04-01__single_stressng', 
        #     title='only stressng',
        #     description='only stressng',
        #     params={'instances_count': 'all machines', 'workloads_count': '1', 'stabilize_phase_length [min]': [20, 1, 1]},
        #     changelog='Setting frequency to 2.1MHz',
        #     bugs=''),

        ExperimentMeta(
            data_path='results/2020-04-01__single_all', 
            title='All workloads',
            description='[part1] All workloads',
            params={'instances_count': 'all machines', 'workloads_count': 'all', 'stabilize_phase_length [min]': [1, 20, 1]},
            changelog='Setting frequency to 2.1MHz again after pepe rebooting machines',
            bugs='Drop after specjbb, please merge with results_2020-04-01__single_all__',
            experiment_type=ExperimentType.SingleWorkloadsRun,
            experiment_baseline_index=1,),

        ExperimentMeta(
            data_path='results/2020-04-01__single_all__', 
            title='All workloads',
            description='[part2] All workloads',
            params={'instances_count': 'all machines', 'workloads_count': 'all', 'stabilize_phase_length [min]': [1, 20, 1]},
            changelog='Setting frequency to 2.1MHz again after pepe rebooting machines',
            bugs='Drop after specjbb, please merge with results_2020-04-01__single_all__',
            experiment_type=ExperimentType.SingleWorkloadsRun,
            experiment_baseline_index=1,),

        ExperimentMeta(
            data_path='results/2020-04-04__stepping_single_workloads', 
            title='Stepping workloads',
            description='First time, almost all workloads',
            params={'instances_count': 'up to 4', 'workloads_count': 'almost all', 'stabilize_phase_length [min]': [20]},
            changelog='',
            bugs='',
            experiment_type=ExperimentType.SteppingSingleWorkloadsRun,
            experiment_baseline_index=0,),

        ExperimentMeta(
            data_path='results/2020-04-13__stepping_single_workloads', 
            title='Stepping workloads',
            description='4 workloads, one to max (with varying step size)',
            params={'instances_count': 'up to max', 'workloads_count': '4', 'stabilize_phase_length [min]': [20]},
            changelog='',
            bugs='',
            experiment_type=ExperimentType.SteppingSingleWorkloadsRun,
            experiment_baseline_index=0,),

        ExperimentMeta(
            data_path='results/2020-04-15__stepping_single_workloads', 
            title='Stepping workloads',
            description='3 workloads, one to max',
            params={'instances_count': 'up to max', 'workloads_count': '4', 'stabilize_phase_length [min]': [20]},
            changelog='better resolution for the workloads (if only possible to run on pmem, run there)',
            bugs='',
            experiment_type=ExperimentType.SteppingSingleWorkloadsRun,
            experiment_baseline_index=0,
            commit_hash='35a1216a516b',)
    ]

    # copy data to summary dir with README
    summary_dir = "__SUMMARY_{}__".format(datetime.datetime.now().strftime('%Y-%m-%d'))
    logging.debug('Saving all results to {}'.format(summary_dir))
    if not os.path.isdir(summary_dir):
        os.mkdir(summary_dir)

    # save changelog
    with open(os.path.join(summary_dir, 'README.CHANGELOG'), 'w') as fref:
        for experiment_meta in experiments_meta:
            fref.write(str(experiment_meta))
            fref.write('\n\n')

    # for each experiment
    for iem, experiment_meta in enumerate(experiments_meta):

        # Run only for last experiment
        if iem < len(experiments_meta) - 1:
            logging.debug('Skipping experiment {}'.format(experiment_meta.data_path))
            continue

        analyze_3stage_experiment(experiment_meta)
        copyfile(os.path.join(experiment_meta.data_path, 'runner_analyzer', 'results.txt'),
                 os.path.join(summary_dir, experiment_meta.data_path_() + '.summary.txt'))
