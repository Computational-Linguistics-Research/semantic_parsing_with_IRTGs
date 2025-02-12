import sys
import re
import numpy as np
from collections import defaultdict
import logging
import logging.handlers
import argparse

from correspondences import ud_4lang_dict
from input_utils import basic_tree_dep_reader
from template import Template

parser = argparse.ArgumentParser(description = "Script for generating IRTG grammars from parallel data")
parser.add_argument("-d", "--dep_format", choices = ["basic", "isi", "conll"], required = True, help = "format of the dependency file")
parser.add_argument("-l", "--log_folder", default = "log" , help = "path to the log folder")
parser.add_argument("-p", "--phrase", required = True, help = "the name of the phrase level at the root of the trees, e.g. NP, S")
parser.add_argument("tree_file", help = "the file containing trees")
parser.add_argument("dep_file", help = "the file containing dependency graphs")

args = parser.parse_args()

logger = None
LOG_FILE_NAME = "{}/generate_grammar.log".format(args.log_folder)


def init_logger():
    global logger
    logger = logging.getLogger('generate_grammar')
    # Levels: DEBUG, INFO, WARNING, ERROR, CRITICAL
    logger.setLevel(logging.DEBUG)

    # Add formatter
    formatter = logging.Formatter(
        "[{asctime}] {levelname}: {message}",
        datefmt="%Y-%m-%d %H:%M:%S",
        style="{"
    )

    # Add rotating file handler
    handler = logging.handlers.RotatingFileHandler(
        LOG_FILE_NAME, maxBytes=1024*1024*5, backupCount=4
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def generate_unary_rules(tree, seen):
    # unary subtrees, e.g. (NP (NNS students))
    for subtree in tree.subtrees(lambda t: len(t) == 1 and 2 < t.height()):
        # holds values used in the template files
        template = Template()
        template.name = "unary"
        template.params = {"treenode": subtree.label()}
        template.rtg_type = "{phrase} -> _{phrase}_unary_{arg}({arg})".format(
            # e.g. NP; left-hand side of the RTG rule
            phrase=subtree.label(),
            # e.g. DT, NN; right-hand side of the RTG rule
            arg=subtree[0].label()
        )
        rule = template.render()
        rtg = rule.split("\n")[0].strip()
        if rtg not in seen:
            seen[rtg]["rule"] = rule
            seen[rtg]["count"] = 1
        else:
            seen[rtg]["count"] += 1



def handle_special_4lang(
    fourlang_edge_type, template, dep, dep_dict, argument_position
):
    if fourlang_edge_type == "CASE":
        handle_4lang_case(
            template, dep, dep_dict, argument_position
        )
    elif fourlang_edge_type.startswith("#"):
        template.name += "4langhas"
        template.params["4lang_edge"] = "1"
        template.params["4lang_edge2"] = "2"
        template.params["4langnode"] = fourlang_edge_type[1:]
    else:
        template.name += "4langnormal"
        template.params["4lang_edge"] = fourlang_edge_type


def init_rtg_args(ancestor_info, template, seen_ternary_nodes):
    replace_arg = None
    """Performs a check whether an RTG argument has to be replaced with a
    technical node name when merging a ternary tree"""
    if "ternary_arg" in ancestor_info:  # happens when merging a ternary tree
        ancestor_hash = get_tree_hash(ancestor_info["common_ancestor"])
        """Decides which argument to replace based on which nodes were first
        connected by a dependency"""
        if seen_ternary_nodes[ancestor_hash]["is_leading_merge"]:
            replace_arg = 1
        else:
            replace_arg = 2
            template.params["treechildren"] = "?2, ?1"

    # set default rtg args
    children = [
        ancestor_info["child2"].label(),
        ancestor_info["child1"].label()
    ]
    rtg_arg1 = children[0] if ancestor_info["is_reverse"] else children[1]
    rtg_arg2 = children[1] if ancestor_info["is_reverse"] else children[0]

    # overwrite rtg args when merging a ternary tree
    rtg_arg1 = ancestor_info["ternary_arg"] if replace_arg == 1 else rtg_arg1
    rtg_arg2 = ancestor_info["ternary_arg"] if replace_arg == 2 else rtg_arg2
    return rtg_arg1, rtg_arg2


def make_rtg_line_old(ancestor_info, rtg_phrase, ud_edge, rtg_arg1, rtg_arg2):
    tpl = "{phrase} -> _{treenode}_{dep}_{arg1}_{arg2}({arg1}, {arg2})"
    treenode = ancestor_info["common_ancestor"].label() + str(
        ancestor_info["arity"]
    )
    return tpl.format(
        treenode=treenode,
        phrase=rtg_phrase,
        dep=ud_edge,
        arg1=rtg_arg1,
        arg2=rtg_arg2
    )



def make_rtg_line(ancestor_info, seen_ternary_nodes, params):
    tpl = "{phrase} -> _{treenode}_{dep}_{arg1}_{arg2}{postfix}({arg1}, {arg2})"

    params["treenode"] = ancestor_info["common_ancestor"].label() + str(
        ancestor_info["arity"]
    )

    params["postfix"] = ""
    if "ternary_phrase" in ancestor_info:
        ancestor_hash = get_tree_hash(ancestor_info["common_ancestor"])
        if seen_ternary_nodes[ancestor_hash]["is_leading_merge"]:
            params["postfix"] = "_leading"
        else:
            params["postfix"] = "_trailing"

    return tpl.format(**params)


def find_dep_tree_correspondences(tree, dep, seen):
    """
    Handles IRTG rule generation given a tree and its corresponding
    dependencies
    """
    """keeps track of ternary nodes in the tree to generate normal or merge
    rules for ternaries"""
    seen_ternary_nodes = defaultdict(dict)

    tree_dict = {}  # index the tree by the words
    # finds the smallest subtrees, e.g. (NNS students)
    for i, smallest_subtree in enumerate(
        tree.subtrees(lambda t: 2 == t.height())
    ):
        smallest_subtree[0] = "{}-{}".format(smallest_subtree[0], i + 1)
        tree_dict[smallest_subtree[0]] = smallest_subtree
    generate_unary_rules(tree, seen)

    # finds a corresponding tree structure for each dependency
    for current_dep in dep:
        template = Template()

        try:
            subtree1 = tree_dict[current_dep["root"]]  # smallest subtree for the word
            subtree2 = tree_dict[current_dep["dep"]]
        except KeyError:
            continue


        ancestor_info = find_common_ancestor(subtree1, subtree2)
        argument_position = {
            ancestor_info["is_reverse"]: "?1",
            not ancestor_info["is_reverse"]: "?2"
        }

        # overwritten later if not true (when merging ternaries)
        template.params = {"treechildren": "?1, ?2"}
        # the node name in the tree interpretation
        template.params["treenode"] = ancestor_info["common_ancestor"].label() + "_" + str(ancestor_info["arity"])

        if ancestor_info["arity"] == 2:  # binary and ternary subtrees
            template.name += "binary_"
            # idáig még eljut
        elif ancestor_info["arity"] == 3:
            """for ternary nodes, finds which adjacent nodes are connected by
            a dependency first"""
            find_first_adjacent_dep(
                ancestor_info["common_ancestor"], seen_ternary_nodes, dep
            )
            handle_ternary(
                template, ancestor_info, seen_ternary_nodes
            )

        template.name += "udnormal_"
        # Set some default template params; they will be overwritten if needed
        template.params["ud_edge"] = current_dep["name"]

        """When the head of the dependency appears later in the tree than
        its dependent, the substituted arguments in the ud and 4lang
        interpretations must represent this difference in order"""
        template.params["ud_root"] = argument_position[False]
        template.params["ud_dep"] = argument_position[True]
        template.params["4lang_root"] = template.params["ud_root"]
        template.params["4lang_dep"] = template.params["ud_dep"]

        """Checks if the dependency needs a special 4lang interpretation
        (edge type, node configuration, etc.)"""
        if current_dep["name"] in ud_4lang_dict:
            fourlang_edge_type = ud_4lang_dict[current_dep["name"]]
            handle_special_4lang(
                fourlang_edge_type, template, dep, current_dep,
                argument_position
            )
        else:
            # Checks if some nodes should be ignored in the 4lang interpretation
            if current_dep["name"] in ["det", "punct"]:
                template.name += "4langignore"
                anc_info = ancestor_info["child1"].label() in ["DT", "PUNCT"]
                template.params["4langnode"] = argument_position[True] if anc_info else argument_position[False]
            else:
                """currently undefined ud-4lang correspondences are marked
                by an "_" edge"""
                template.name += "4langnormal"
                template.params["4lang_edge"] = "_"

        # RTG rule generation

        """if ternary_phrase is set in ancestor_info, we have a ternary rule
        and the left-hand side needs to be replaced with the previously
        generated technical node name"""
        rtg_phrase = ancestor_info["common_ancestor"].label()
        if "ternary_phrase" in ancestor_info:
            rtg_phrase = ancestor_info["ternary_phrase"]

        try:
            rtg_arg1, rtg_arg2 = init_rtg_args(
                ancestor_info, template, seen_ternary_nodes
            )
        except KeyError:
            continue

        # Generate RTG rule line
        template.rtg_type = make_rtg_line_old(
            ancestor_info, rtg_phrase, current_dep["name"], rtg_arg1, rtg_arg2
        )
        # template.rtg_type = make_rtg_line(ancestor_info, seen_ternary_nodes, {
        #    "phrase": rtg_phrase,
        #    "dep": current_dep["name"],
        #    "arg1": rtg_arg1,
        #    "arg2": rtg_arg2,
        # })

        if "_skip" in template.params:
            continue

        try:
            rule = template.render()
        except FileNotFoundError:
            continue

        rtg = rule.split("\n")[0].strip()
        if rtg not in seen:
            seen[rtg]["rule"] = rule
            seen[rtg]["count"] = 1
        else:
            seen[rtg]["count"] += 1


def handle_4lang_case(template, dep_list, current_dep, is_reverse):
    """
    Calculates information relating to the case dependency for the
    4lang interpretation
    """

    should_skip = True
    case_head = current_dep["root"]


    """Finds a relevant dependency connected to this case dependency
    (it never appears alone)"""
    for dep in dep_list:
        """Cases where the head of the case dep is the dependent of the
        related dependency"""
        if len(dep_list) == 1 and dep["name"] == "case":
            template.params["_skip"] = True

        if case_head == dep["dep"]:
            # cases where a node is not represented in 4lang
            if dep["name"] in [
                "nmod", "nmod_poss", "nmod_of", "nmod_including", "nmod_at",
                "nmod_on", "nmod_since", "nmod_from", "nmod_such_as",
                "nmod_but"
            ]:
                should_skip = False
                template.name += "4langignore"
                template.params["4langnode"] = is_reverse[False]
            elif dep["name"] in ["obl", "nmod_in", "nmod_to"]:
                should_skip = False
                set_template_params("2", template, is_reverse)
        elif case_head == dep["root"] and dep["name"] != "case" and dep["name"] == "nsubj":
            """Cases where the head of the case dep is the head of the related
            dependeny (case dependencies should be ignored)"""
            should_skip = False
            set_template_params("1", template, is_reverse)

    if should_skip:
        template.params["_skip"] = True
        template.name += "udnormal"


def set_template_params(number, template, is_reverse):
    template.params["4lang_edge"] = number
    template.params["4lang_root"] = is_reverse[True]
    template.params["4lang_dep"] = is_reverse[False]
    template.name += "4langnormal"


def handle_ternary(template, ancestor_info, seen_ternary_nodes):
    """
    Calculates information related to ternary node rules
    """
    common_ancestor = ancestor_info["common_ancestor"]
    """technical node name used in the left and right-hand side of the
    RTG rules handling ternary nodes"""
    technical_node = common_ancestor.label() + "_MERGE"
    ancestor_hash = get_tree_hash(common_ancestor)
    """Checks whether the "seen" flag has been set for a given node in
    seen_ternary_nodes"""
    is_ternary_node_seen = seen_ternary_nodes[ancestor_hash].get("seen", False)

    """
    If the connected nodes are adjacent and the parent has not been processed
    yet, signal that a rule needs to be created that creates a ternary node in
    the tree interpretation.
    Otherwise a binary merge rule needs to be created later.
    Also some relating information needs to be calculated.
    """
    if ancestor_info["is_adjacent"] and not is_ternary_node_seen:
        # mark this node as seen
        seen_ternary_nodes[ancestor_hash]["seen"] = True
        template.name += "ternary_"
        """in the ternary templates, the left-hand side phrase must be
        replaced with the technical node name"""
        ancestor_info["ternary_phrase"] = technical_node

        # decide where to put the node in the ternary that will be merged later
        template.params["treechildren"] = "?1, ?2, *" if ancestor_info["is_leading_merge"] else "*, ?1, ?2"
    else:
        template.name += "binary_"
        """in a merge rule, one of the RTG arguments must be replaced by
        this technical node"""
        ancestor_info["ternary_arg"] = technical_node
        # the treenode in the template must be replaced by the merge operation
        template.params["treenode"] = "@"


def find_first_adjacent_dep(treenode, seen_ternary_nodes, deps):
    """
    Finds the first adjacent dependency link between the children of a
    ternary treenode
    """
    # Get the list of words contained in each of the nodes children
    child1_leaves = treenode[0].leaves()
    child2_leaves = treenode[1].leaves()
    child3_leaves = treenode[2].leaves()

    treenode_hash = get_tree_hash(treenode)

    """iterate through all the dependecies in the tree and return as soon as
    the first link is found"""
    for d in deps:
        """there can only be an adjacent link if one of the dependecy words
        belong to the second child"""
        if d["root"] in child2_leaves or d["dep"] in child2_leaves:
            if d["root"] in child1_leaves or d["dep"] in child1_leaves:
                # the link is between the first and second child
                seen_ternary_nodes[treenode_hash]["is_leading_merge"] = True
                return
            elif d["root"] in child3_leaves or d["dep"] in child3_leaves:
                # the link is between the second and the third child
                seen_ternary_nodes[treenode_hash]["is_leading_merge"] = False
                return


def get_tree_hash(treenode):
    """
    NLTK trees are not hashable, this calculates a "hash" as a workaround,
    so they can be keys in a dictionary
    """
    return treenode.pformat(100000)  # it returns a string


def find_common_ancestor(subtree1, subtree2):
    """
    finds the common ancestor for the given subtrees and some other
    relevant information
    """
    """tuples of indexes representing the positions of the subtrees in
    the main tree"""
    pos_tuple1 = subtree1.treeposition()
    pos_tuple2 = subtree2.treeposition()
    # the root node must be a common ancestor
    common_ancestor = subtree1.root()

    # iterates through the position indexes simultaneously
    for pos1, pos2 in zip(pos_tuple1, pos_tuple2):
        """if the positions are different, the previously found common ancestor
        is the lowest common ancestor, stop iterating over the positions"""
        if pos1 != pos2:
            break
        # if the positions match, a new common ancestor is found along the tree
        common_ancestor = common_ancestor[pos1]

    result = {
        "common_ancestor": common_ancestor,
        # if pos1 > pos2, the head of the dependency appears later in
        # the tree than its dependent (based on treeposition indexes)
        "is_reverse": pos1 > pos2,
        "child1": common_ancestor[pos1],
        "child2": common_ancestor[pos2],
        "arity": len(common_ancestor),
        # for adjacent nodes pos1-pos2 is either 1 or -1
        "is_adjacent": abs(pos1 - pos2) == 1,
        # whether the 1st two nodes are connected by the dependency in
        # a ternary (when is_adjacent is True)
        "is_leading_merge": pos1 == 0 or pos2 == 0,
    }

    return result


def print_interpretation():
    # interpretation definitions
    print("interpretation string: de.up.ling.irtg.algebra.StringAlgebra")
    print("interpretation tree: de.up.ling.irtg.algebra.TagTreeAlgebra")
    print("interpretation ud: de.up.ling.irtg.algebra.graph.GraphAlgebra")
    print("interpretation fourlang: de.up.ling.irtg.algebra.graph.GraphAlgebra")
    print()


def print_start_rule():
    # start rule for NPs
    print("S! -> sentence({})".format(args.phrase))
    print("[string] ?1")
    print("[tree] ?1")
    print("[ud] ?1")
    print("[fourlang] ?1")
    print()


def sort_by_value(dep_dict):
    sorted_by_value = sorted(dep_dict.items(), key = lambda kv: -kv[1]["count"])
    values = [value[1]["count"] for value in sorted_by_value]
    values.reverse()

    data_mean, data_std = np.mean(values), np.std(values)

    q1, q3= np.percentile(values,[25,75])

    counts = [c[1]["count"] for c in sorted_by_value]

    max_count = sum(counts)

    for i in sorted_by_value:
         #if i[1]["count"] >= 30:
            print("// {}".format(i[1]["count"]))
            split_i = i[1]["rule"].split("\n")
            print(split_i[0] + " [" + str(i[1]["count"]/max_count) + "]")
            print(split_i[1])
            print(split_i[2])
            print(split_i[3])
            print(split_i[4])
            print()


def main(fn1, fn2):
    init_logger()
    seen = defaultdict(dict)
    print_interpretation()
    print_start_rule()
    # iterates through trees and their corresponding dependencies
    for tree, dep in basic_tree_dep_reader(fn1, fn2, args.dep_format):
        logger.debug("Processing tree: {}".format(tree.pformat(10000)))
        find_dep_tree_correspondences(tree, dep, seen)
    sort_by_value(seen)


if __name__ == "__main__":
    main(args.tree_file, args.dep_file)
