#!/usr/bin/env python

"""
The slow version of the OSPF synthesizer.
"""

from timeit import default_timer as timer

import networkx as nx
import z3

from synet.utils.common import NODE_TYPE
from synet.utils.common import PathReq
from synet.utils.common import SetOSPFEdgeCost
from synet.utils.common import SynthesisComponent
from synet.utils.common import VERTEX_TYPE

__author__ = "Ahmed El-Hassany"
__email__ = "a.hassany@gmail.com"


z3.set_option('unsat-core', True)


class OSPFSyn(SynthesisComponent):
    """
    Synthesizer for OSPF costs, this version validate the complete network model,
    that makes it a slow but complete version.
    """

    valid_inputs = (SetOSPFEdgeCost,)
    def __init__(self, initial_configs, network_graph, solver=None):
        """
        :param initial_configs: List of SetOSPFEdgeCost, ignores anything else
        :param network_graph: an instance of Networkx.DiGraph
        :param solver: optional instance of Z3 solver, otherwise create an new one
        """
        super(OSPFSyn, self).__init__(initial_configs, network_graph, solver)
        self._load_graph(network_graph)
        self.solver = solver if solver else z3.Solver()
        self.initial_configs = initial_configs if initial_configs else []
        # Read vertices
        self._create_vertices('OSPFVertex', self.initial_configs,
                              self.network_graph, True)
        # True is an edge exists between two vertices
        self.edge = z3.Function('EdgePhyOSPF', self.vertex,
                                self.vertex, z3.BoolSort())
        # Assign a cost to each edge
        self.edge_cost = z3.Function('OSPFEdgeCost', self.vertex, self.vertex,
                                     z3.IntSort())
        # Read input
        self._read_input_graph()
        # Requirements
        self.reqs = []

    def _load_graph(self, network_graph):
        """
        Read the network graph, OSPF care only about nodes marked as NODE_TYPE
        Other nodes, such as Peers are ignored.
        """
        g = network_graph.copy() if network_graph else nx.DiGraph()
        # Only local routers
        for node, data in list(g.nodes(data=True))[:]:
            if data[VERTEX_TYPE] != NODE_TYPE:
                del g.node[node]
        self.network_graph = g

    def _read_input_graph(self):
        """
        Reads the inputs and add them as constraints to the solver
        """
        # First annotate the network graph with any given costs
        for tmp in self.initial_configs:
            if isinstance(tmp, SetOSPFEdgeCost):
                self.network_graph.edge[tmp.src][tmp.dst]['cost'] = int(tmp.cost)
        # Stop the solver from adding a new edges
        for src in self.network_graph.nodes():
            if src in self.network_names:
                continue
            for dst in self.network_graph.nodes():
                if dst in self.network_names:
                    continue
                src_v = self.get_vertex(src)
                dst_v = self.get_vertex(dst)
                if self.network_graph.has_edge(src, dst):
                    cost = self.network_graph.edge[src][dst].get('cost', None)
                    self.solver.add(self.edge(src_v, dst_v))
                    if cost:
                        self.solver.add(self.edge_cost(src_v, dst_v) == cost)
                    else:
                        self.solver.add(self.edge_cost(src_v, dst_v) >= 0)
                else:
                    self.solver.add(z3.Not(self.edge(src_v, dst_v)))

    def add_path_req(self, req):
        """
        Add new path requirement
        :param req: instance of PathReq
        :return: None
        """
        assert isinstance(req, PathReq)
        self.reqs.append(req)

    def _get_edge_cost(self, src, dst):
        """Shortcut function to get the cost function of an edge"""
        cost = self.network_graph[src][dst].get('cost', 0)
        if cost > 0:
            return cost
        else:
            src = self.get_vertex(src)
            dst = self.get_vertex(dst)
            return self.edge_cost(src, dst)

    def _get_path_cost(self, path):
        """Return a sum of all the costs along the path"""
        edge_costs = []
        for i in range(len(path) - 1):
            src = path[i]
            dst = path[i + 1]
            #self.solver.add(self.edge(self.get_vertex(src), self.get_vertex(dst)))
            edge_costs.append(self._get_edge_cost(src, dst))
        return sum(edge_costs)

    def push_requirements(self):
        """Push the requirements we care about to the solver"""
        self.solver.push()
        start = timer()
        for req in self.reqs:
            if isinstance(req, PathReq):
                path = req.path
                src = path[0]
                dst = path[-1]
                path_cost = self._get_path_cost(path)
                # Enumerate all paths
                for sp in nx.all_simple_paths(self.network_graph, src, dst):
                    if path != sp:
                        simple_path_cost = self._get_path_cost(sp)
                        self.solver.add(path_cost < simple_path_cost)
        end = timer()
        return end - start

    def get_output_configs(self):
        m = self.solver.model()
        outputs = []
        check = lambda x: z3.is_true(m.eval(x))
        for src, src_v in self.name_to_vertex.iteritems():
            for dst, dst_v in self.name_to_vertex.iteritems():
                if not check(self.edge(src_v, dst_v)):
                    continue
                cost = m.eval(self.edge_cost(src_v, dst_v)).as_long()
                outputs.append(SetOSPFEdgeCost(src, dst, cost))
        return outputs

    def get_output_network_graph(self):
        m = self.solver.model()
        g = nx.DiGraph()
        check = lambda x: z3.is_true(m.eval(x))
        for src, src_v in self.name_to_vertex.iteritems():
            g.add_node(src,
                       **self._get_vertex_attributes(self.network_graph, src_v))
            for dst, dst_v in self.name_to_vertex.iteritems():
                if not g.has_node(dst):
                    g.add_node(dst,
                               **self._get_vertex_attributes(self.network_graph,
                                                             dst_v))
                if check(self.edge(src_v, dst_v)):
                    cost = m.eval(self.edge_cost(src_v, dst_v)).as_long()
                    g.add_edge(src, dst, cost=cost,
                               **self._get_edge_attributes(src_v, dst_v))

        return g

    def get_output_routing_graphs(self):
        """
        Returns one graph per each destination network.
        """
        m = self.solver.model()
        g_phy = self.get_output_network_graph()
        graphs = {}
        for dst_net in self.networks:
            dst_net_name = self.get_name(dst_net)
            g = nx.DiGraph()
            g.graph['dst'] = dst_net_name
            graphs[dst_net_name] = g
            for node, _ in self.name_to_vertex.iteritems():
                if not nx.has_path(g_phy, node, dst_net_name):
                    continue
                path = nx.shortest_path(g_phy, node, dst_net_name, 'cost')
                for i in range(len(path) - 1):
                    src = path[i]
                    dst = path[i + 1]
                    if not g.has_node(src):
                        g.add_node(src,
                                   **self._get_vertex_attributes(g_phy, src))
                    if not g.has_node(dst):
                        g.add_node(dst,
                                   **self._get_vertex_attributes(g_phy, dst))

                    cost = m.eval(
                        self.edge_cost(self.get_vertex(src),
                                       self.get_vertex(dst))).as_long()
                    g.add_edge(src, dst, cost=cost,
                               **self._get_edge_attributes(src, dst))
        return graphs