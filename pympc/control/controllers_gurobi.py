# external imports
import numpy as np
import gurobipy as grb
from collections import OrderedDict

# internal inputs
from pympc.geometry.polyhedron import Polyhedron
from pympc.optimization.programs import linear_program
from pympc.optimization.solvers.branch_and_bound import Tree
from pympc.optimization.solvers.gurobi import linear_expression, quadratic_expression

class HybridModelPredictiveController(object):

    def __init__(self, S, N, Q, R, P, X_N, method='big_m'):

        # store inputs
        self.S = S
        self.N = N
        self.Q = Q
        self.R = R
        self.P = P
        self.X_N = X_N

        # mpMIQP
        self.prog = self.build_mpmiqp(method) # adds the variables: prog, objective, u, x, d, initial_condition
        self.partial_mode_sequence = []

    def build_mpmiqp(self, method):

        # shortcuts
        [nx, nu, nm] = [self.S.nx, self.S.nu, self.S.nm]

        # express the constrained dynamics as a list of polytopes in the (x,u,x+)-space
        P = get_graph_representation(self.S)

        # get big-Ms for some of the solution methods
        m, mi = get_big_m(P)

        # initialize program
        prog = grb.Model()
        obj = 0.

        # parameters
        prog.setParam('OutputFlag', 0)
        prog.setParam('Method', 0)

        # loop over time
        for t in range(self.N):

            # initial conditions (set arbitrarily to zero in the building phase)
            if t == 0:
                x = prog.addVars(nx, lb=[0.]*nx, ub=[0.]*nx, name='x0')

            # create stage variables
            else:
                x = x_next
            x_next = prog.addVars(nx, lb=[-grb.GRB.INFINITY]*nx, name='x%d'%(t+1))
            u = prog.addVars(nu, lb=[-grb.GRB.INFINITY]*nu, name='u%d'%t)
            d = prog.addVars(nm, name='d%d'%t)

            # auxiliary continuous variables for the convex-hull method
            if method == 'convex_hull':
                y = prog.addVars(nm, nx, lb=[-grb.GRB.INFINITY]*nm*nx, name='y%d'%t)
                z = prog.addVars(nm, nx, lb=[-grb.GRB.INFINITY]*nm*nx, name='z%d'%t)
                v = prog.addVars(nm, nu, lb=[-grb.GRB.INFINITY]*nm*nu, name='v%d'%t)
            prog.update()

            # enforce constrained dynamics (big-m methods)
            if method in ['big_m', 'improved_big_m']:
                xux = np.array(x.values() + u.values() + x_next.values())
                for i in range(nm):
                    if method == 'big_m':
                        for k in range(P[i].A.shape[0]):
                            prog.addConstr(P[i].A[k].dot(xux) <= P[i].b[k,0] + mi[i][k,0] * (1. - d[i]))
                    if method == 'improved_big_m':
                        sum_mi = sum(m[i][j] * d[j] for j in range(self.S.nm) if j != i)
                        for k in range(P[i].A.shape[0]):
                            prog.addConstr(P[i].A[k].dot(xux) <= P[i].b[k,0] + sum_mi[k,0])

            # enforce constrained dynamics (convex hull method)
            elif method == 'convex_hull':
                for i in range(nm):
                    yvyi = np.array(
                        [y[i,k] for k in range(nx)] +
                        [v[i,k] for k in range(nu)] +
                        [z[i,k] for k in range(nx)]
                        )
                    for k in range(P[i].A.shape[0]):
                        prog.addConstr(P[i].A[k].dot(yvyi) <= P[i].b[k,0] * d[i])

                # recompose the state and input (convex hull method)
                for k in range(nx):
                    prog.addConstr(x[k] == sum(y[i,k] for i in range(nm)))
                    prog.addConstr(x_next[k] == sum(z[i,k] for i in range(nm)))
                for k in range(nu):
                    prog.addConstr(u[k] == sum(v[i,k] for i in range(nm)))

            # raise error for unknown method
            else:
                raise ValueError('unknown method ' + method + '.')

            # constraints on the binaries
            prog.addConstr(sum(d.values()) == 1.)

            # stage cost to the objective
            obj += .5 * np.array(u.values()).dot(self.R).dot(np.array(u.values()))
            obj += .5 * np.array(x.values()).dot(self.Q).dot(np.array(x.values()))

        # terminal constraint
        for k in range(self.X_N.A.shape[0]):
            prog.addConstr(self.X_N.A[k].dot(np.array(x_next.values())) <= self.X_N.b[k,0])

        # terminal cost
        obj += .5 * np.array(x_next.values()).dot(self.P).dot(np.array(x_next.values()))
        prog.setObjective(obj)

        return prog

    def set_initial_condition(self, x0):
        for k in range(self.S.nx):
            self.prog.getVarByName('x0[%d]'%k).LB = x0[k,0]
            self.prog.getVarByName('x0[%d]'%k).UB = x0[k,0]

    def update_mode_sequence(self, partial_mode_sequence):

        # loop over the time horizon
        for t in range(self.N):

            # write and erase
            if t < len(partial_mode_sequence) and t < len(self.partial_mode_sequence):
                if partial_mode_sequence[t] != self.partial_mode_sequence[t]:
                    self.prog.getVarByName('d%d[%d]'%(t,self.partial_mode_sequence[t])).LB = 0.
                    self.prog.getVarByName('d%d[%d]'%(t,partial_mode_sequence[t])).LB = 1.

            # erase only
            elif t >= len(partial_mode_sequence) and t < len(self.partial_mode_sequence):
                self.prog.getVarByName('d%d[%d]'%(t,self.partial_mode_sequence[t])).LB = 0.

            # write only
            elif t < len(partial_mode_sequence) and t >= len(self.partial_mode_sequence):
                self.prog.getVarByName('d%d[%d]'%(t,partial_mode_sequence[t])).LB = 1.

        # update partial mode sequence
        self.partial_mode_sequence = partial_mode_sequence

    def solve_relaxation(self, partial_mode_sequence, cutoff_value=None, warm_start=None):

        # self.prog.reset()

        # warm start for active set method
        if warm_start is not None:
            for i, v in enumerate(self.prog.getVars()):
                v.VBasis = warm_start['variable_basis'][i]
            for i, c in enumerate(self.prog.getConstrs()):
                c.CBasis = warm_start['constraint_basis'][i]
        self.prog.update()

        # set cut off from best upper bound
        if cutoff_value is not None:
            self.prog.setParam('Cutoff', cutoff_value)

        # fix part of the mode sequence
        self.update_mode_sequence(partial_mode_sequence)

        # run the optimization
        self.prog.optimize()
        result = dict()
        result['solve_time'] = self.prog.Runtime

        # check status
        result['cutoff'] = read_gurobi_status(self.prog.status) == 'cutoff'
        if result['cutoff']:
        	result['feasible'] = None
        else:
        	result['feasible'] = read_gurobi_status(self.prog.status) == 'optimal'

        # return if cutoff or unfeasible
        if result['cutoff'] or not result['feasible']:
        	return result

        # store argmin in list of vectors
        result['x'] = [[self.prog.getVarByName('x%d[%d]'%(t,k)).x for k in range(self.S.nx)] for t in range(self.N+1)]
        result['u'] = [[self.prog.getVarByName('u%d[%d]'%(t,k)).x for k in range(self.S.nu)] for t in range(self.N)]
        d = [[self.prog.getVarByName('d%d[%d]'%(t,k)).x for k in range(self.S.nm)] for t in range(self.N)]

        # retrieve mode sequence and check integer feasibility
        result['mode_sequence'] = [dt.index(max(dt)) for dt in d]
        result['integer_feasible'] = all([np.allclose(sorted(dt), [0.]*(len(dt)-1)+[1.]) for dt in d])

        # heuristic to guess the optimal mode at the first relaxed time step
        if len(partial_mode_sequence) < self.N:
            result['children_order'], result['children_score'] = self.mode_heuristic(d)
        else:
            result['children_order'] = None
            result['children_score'] = None

        # other solver outputs
        result['cost'] = self.prog.objVal
        result['variable_basis'] = [v.VBasis for v in self.prog.getVars()]
        result['constraint_basis'] = [c.CBasis for c in self.prog.getConstrs()]

        return result

    def mode_heuristic(self, d):

        # order by the value of the relaxed binaries
        children_score = d[len(self.partial_mode_sequence)]
        children_order = np.argsort(children_score)[::-1].tolist()

        # put in fron the mode of the parent node
        if len(self.partial_mode_sequence) > 0:
            children_order.insert(0, children_order.pop(children_order.index(self.partial_mode_sequence[-1])))

        return children_order, children_score

    def feedforward(self, x0, draw_solution=False):

        # overwrite initial condition
        self.set_initial_condition(x0)

        # call branch and bound algorithm
        tree = Tree(self.solve_relaxation)
        tree.explore()

        # draw the tree
        if draw_solution:
            tree.draw()

        # output
        if tree.incumbent is None:
            return [None]*4
        else:
            return [tree.incumbent.result[key] for key in ['u', 'x', 'mode_sequence', 'cost']]

    def feedforward_gurobi(self, x0):

        # set up miqp
        self.set_d_type('B')
        self.update_mode_sequence([])
        self.set_initial_condition(x0)

        # run the optimization
        self.prog.setParam('OutputFlag', 1)
        self.prog.optimize()
        self.set_d_type('C')

        # output
        if read_gurobi_status(self.prog.status) == 'optimal':
            x = [[self.prog.getVarByName('x%d[%d]'%(t,k)).x for k in range(self.S.nx)] for t in range(self.N+1)]
            u = [[self.prog.getVarByName('u%d[%d]'%(t,k)).x for k in range(self.S.nu)] for t in range(self.N)]
            d = [[self.prog.getVarByName('d%d[%d]'%(t,k)).x for k in range(self.S.nm)] for t in range(self.N)]
            ms = [dt.index(max(dt)) for dt in d]
            cost = self.prog.objVal
            return u, x, ms, cost
        else:
            return [None]*4

    def set_d_type(self, d_type):
        for t in range(self.N):
            for i in range(self.S.nm):
                self.prog.getVarByName('d%d[%d]'%(t,i)).VType = d_type

def get_graph_representation(S):
    P = []
    for i in range(S.nm):
        Di = S.domains[i]
        Si = S.affine_systems[i]
        Ai = np.vstack((
            np.hstack((Di.A, np.zeros((Di.A.shape[0], S.nx)))),
            np.hstack((Si.A, Si.B, -np.eye(S.nx))),
            np.hstack((-Si.A, -Si.B, np.eye(S.nx))),
            ))
        bi = np.vstack((Di.b, -Si.c, Si.c))
        P.append(Polyhedron(Ai, bi))
    return P

def get_big_m(P_list, tol=1.e-6):
    m = []
    for i, Pi in enumerate(P_list):
        mi = []
        for j, Pj in enumerate(P_list):
            mij = []
            for k in range(Pi.A.shape[0]):
                f = -Pi.A[k:k+1,:].T
                sol = linear_program(f, Pj.A, Pj.b)
                mijk = - sol['min'] - Pi.b[k,0]
                if np.abs(mijk) < tol:
                    mijk = 0.
                mij.append(mijk)
            mi.append(np.vstack(mij))
        m.append(mi)
    mi = [np.maximum.reduce([mij for mij in mi]) for mi in m]
    return m, mi

def read_gurobi_status(status):
	return {
		1: 'loaded', # Model is loaded, but no solution information is available.'
		2: 'optimal',	# Model was solved to optimality (subject to tolerances), and an optimal solution is available.
		3: 'infeasible', # Model was proven to be infeasible.
		4: 'inf_or_unbd', # Model was proven to be either infeasible or unbounded. To obtain a more definitive conclusion, set the DualReductions parameter to 0 and reoptimize.
		5: 'unbounded', # Model was proven to be unbounded. Important note: an unbounded status indicates the presence of an unbounded ray that allows the objective to improve without limit. It says nothing about whether the model has a feasible solution. If you require information on feasibility, you should set the objective to zero and reoptimize.
		6: 'cutoff', # Optimal objective for model was proven to be worse than the value specified in the Cutoff parameter. No solution information is available. (Note: problem might also be infeasible.)
		7: 'iteration_limit', # Optimization terminated because the total number of simplex iterations performed exceeded the value specified in the IterationLimit parameter, or because the total number of barrier iterations exceeded the value specified in the BarIterLimit parameter.
		8: 'node_limit', # Optimization terminated because the total number of branch-and-cut nodes explored exceeded the value specified in the NodeLimit parameter.
		9: 'time_limit', # Optimization terminated because the time expended exceeded the value specified in the TimeLimit parameter.
		10: 'solution_limit', # Optimization terminated because the number of solutions found reached the value specified in the SolutionLimit parameter.
		11: 'interrupted', # Optimization was terminated by the user.
		12: 'numeric', # Optimization was terminated due to unrecoverable numerical difficulties.
		13: 'suboptimal', # Unable to satisfy optimality tolerances; a sub-optimal solution is available.
		14: 'in_progress', # An asynchronous optimization call was made, but the associated optimization run is not yet complete.
		15: 'user_obj_limit' # User specified an objective limit (a bound on either the best objective or the best bound), and that limit has been reached.
		}[status]